"""Canonical JSON task state: index ownership, detail files, and pure operations.

State layout inside a checked-out state revision::

    index.json
    items/<id>.json

``index.json`` is ``{"schema_version": 3, "items": {id: entry}, "batches": {}}`` where each
entry owns exactly ``title``, ``priority``, ``status``, ``claim``, and optional
``needs`` IDs. Detail files carry descriptions, acceptance criteria, links,
and retained task context. Detail files must not contain independently
editable copies of index-owned fields. An optional detail-owned
``not_before`` (UTC ISO Z) holds an open task out of the ready queue until
that moment; the status stays ``open`` so the task returns on its own.

Lifecycle: ``open``, ``active``, ``blocked``, ``done``, ``dropped``.
Priorities preserve the historical bands: ``critical``, ``high``,
``medium-high``, ``medium``, ``low``.

A claim is ``{"worker": str, "expires_at": UTC ISO Z, "generation": uuid4hex,
"renewals": int}``. The counter starts at 0 on take and increments on every
renewal or restart re-adoption, so a renewal is always a state change and
the renewal path stays observable.
Ownership plus generation checks protect renew, release, and finish from
stale writers. Claims are cooperative concurrency controls, not
authentication against a malicious repository writer: every writer must run
the publication protocol in :mod:`tractable.git_backend`, and branch
rewrites/deletion invalidate its assumptions. Clock assumption: writers'
clocks are roughly synchronized (minutes); the default TTL is 24h so small
skew is harmless.

This module is pure (no subprocess, no network): it operates on in-memory
``(index, details)`` snapshots plus atomic local-directory IO. Git
publication lives in :mod:`tractable.git_backend`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import hashlib
import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import (
    E_ACTIVE_CLAIMS,
    E_BATCH_HEAD,
    E_BATCH_OWNER,
    E_CLAIM_STALE,
    E_CONFLICT,
    E_FINAL_EVIDENCE,
    E_OPEN_DEFERRALS,
    E_SCHEMA,
    E_STATE,
    TodoError,
)

SCHEMA_VERSION = 4
#: New state branches start here; only the first ``register_batch`` with the
#: confirm flag moves a store to ``SCHEMA_VERSION``.
BOOTSTRAP_SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, SCHEMA_VERSION)
BATCH_CAPABILITY_VERSION = 2
BATCH_SCHEMA = 2
BATCH_TOOLS = ("advance_batch_head", "recover_batch_owner", "show_batch")
DEVELOP_REF_PREFIX = "refs/remotes/"
ADVANCE_REASONS = ("fix", "develop-merge")


def batch_capability(tool_version: str) -> dict[str, Any]:
    """The advertised batch v2 capability (initialize and batch reads)."""
    return {
        "version": BATCH_CAPABILITY_VERSION,
        "receipt_schema": PREPARED_RECEIPT_SCHEMA,
        "store_schema_version": SCHEMA_VERSION,
        "supported_schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
        "tool_version": tool_version,
        "tools": sorted(BATCH_TOOLS),
        "features": {
            "integration_worktree_relocation": True,
        },
    }
STATUSES = ("open", "active", "blocked", "done", "dropped")
PRIORITIES = ("critical", "high", "medium-high", "medium", "low")
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}
TERMINAL_STATUSES = ("done", "dropped")

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# Keep the slug safe for the item filename while accepting IDs from the
# legacy tracker, whose longest existing IDs are 82 characters.
MAX_ID_LEN = 128
MAX_TITLE_LEN = 200
MAX_WORKER_LEN = 128
MAX_SESSION_LEN = 256
MAX_CLIENT_LEN = 64
#: Bound for operation names accepted leniently by session-history readers.
MAX_OP_LEN = 64
#: Bound for takeover reasons: long enough to name the dead session and
#: why it cannot resume, short enough to stay quoted in commit subjects.
MAX_REASON_LEN = 280

#: Unicode line/paragraph separators: invisible, pass an ``ord < 32`` check,
#: but split lines for Git trailer parsing. Forbidden in every identity so a
#: crafted value can never forge a trailer line.
FORBIDDEN_BREAKS = frozenset({"\x85", "\u2028", "\u2029"})  # NEL, LS, PS


def _has_forbidden_breaks(text: str) -> bool:
    return any(char in FORBIDDEN_BREAKS for char in text)


_CONTROL_OR_BREAK_RE = re.compile(r"[\x00-\x1f\x85\u2028\u2029]")


def _has_control_or_break(text: str) -> bool:
    return _CONTROL_OR_BREAK_RE.search(text) is not None
DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 72.0

INDEX_FILENAME = "index.json"
ITEMS_DIRNAME = "items"

#: Fields owned by the index. Detail files containing any of these are
#: rejected so there is never an ambiguous second copy to merge.
INDEX_OWNED_FIELDS = frozenset({"title", "priority", "status", "claim", "needs"})

# Prepared receipts are detail-owned.  They deliberately do not add a second
# status: an item is still open after its claim is handed back, while the
# receipt records the work that is available to an explicitly registered
# same-batch implementation edge.
PREPARED_RECEIPT_SCHEMA = "prepared_work_v2"
LEGACY_RECEIPT_SCHEMA = "prepared_work_v1"
REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SCOPE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

#: Status moves allowed to plain ``update``. Entering ``active`` needs a claim
#: (take); leaving ``active`` releases or closes it (release/finish);
#: terminal states go through finish/drop. Anything else is rejected so the
#: claim protocol cannot be bypassed.
UPDATE_STATUSES = frozenset({"open", "blocked"})

#: Bounds for deferral text. Summaries double as default promotion
#: titles, so they share the item title bound. Reasons justify
#: resolution and may span lines; trailer forgery is not a risk here
#: because deferral text never lands in a commit trailer (only the op
#: summary does, quoted separately).
MAX_DEFERRAL_SUMMARY_LEN = 200
MAX_DEFERRAL_REASON_LEN = 2000

#: Deferral resolutions. `open` is the only actionable state; `promoted`
#: and `dismissed` are terminal and record how the deferral left the queue.
DEFERRAL_RESOLUTIONS = ("open", "promoted", "dismissed")

#: Item-level operations this version writes into the per-task session
#: history. `abort`/`invalidate` name the member tasks a batch lifecycle
#: path rewrote; register/bind touch no member details and stay
#: trailer-only. `defer`/`promote`/`dismiss` record deferral resolution on
#: the source task in the same way. Readers stay lenient — an unknown op
#: is a bounded token, never a load failure — so a future operation cannot
#: strand older clients; only writers are bound to this allowlist.
SESSION_OPS = frozenset({
    "create", "take", "renew", "release", "prepare", "finish", "drop", "update",
    "abort", "invalidate", "takeover", "defer", "promote", "dismiss", "recover",
})

OP_ID_RE = re.compile(r"^[0-9a-f]{32}$")
GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_sessions(item_id: str, sessions: Any) -> None:
    """Validate the append-only per-task session history.

    Absence is valid (pre-history items); presence must be a list of
    entries keyed by unique operation IDs so retries can never duplicate
    an entry and any entry resolves to its state commit via reconcile.
    Unknown extra keys are tolerated for forward compatibility.
    """
    if not isinstance(sessions, list):
        raise TodoError(f"sessions log for {item_id!r} must be a list", code=E_STATE)
    seen: set[str] = set()
    for pos, entry in enumerate(sessions):
        if not isinstance(entry, dict):
            raise TodoError(f"sessions log for {item_id!r} entry {pos} must be an object", code=E_STATE)
        for key in ("actor", "session_id", "op", "at", "op_id"):
            if key not in entry:
                raise TodoError(
                    f"sessions log for {item_id!r} entry {pos} is missing {key!r}", code=E_STATE
                )
        validate_worker(str(entry["actor"]))
        validate_session_id(str(entry["session_id"]))
        if entry.get("client") is not None:
            validate_client_name(str(entry["client"]))
        op = entry["op"]
        # Lenient by design (see SESSION_OPS): a future operation must
        # remain loadable by older readers. Only shape is enforced here.
        if (
            not isinstance(op, str)
            or not op.strip()
            or len(op) > MAX_OP_LEN
            or _has_control_or_break(op)
        ):
            raise TodoError(
                f"sessions log for {item_id!r} entry {pos} has an invalid operation",
                code=E_STATE,
            )
        parse_ts(str(entry["at"]))
        op_id = entry["op_id"]
        if not isinstance(op_id, str) or not OP_ID_RE.fullmatch(op_id):
            raise TodoError(
                f"sessions log for {item_id!r} entry {pos} has an invalid operation ID",
                code=E_STATE,
            )
        generation = entry.get("generation")
        if generation is not None and (
            not isinstance(generation, str) or not GENERATION_RE.fullmatch(generation)
        ):
            raise TodoError(
                f"sessions log for {item_id!r} entry {pos} has an invalid generation",
                code=E_STATE,
            )
        if entry.get("reason") is not None:
            validate_takeover_reason(str(entry["reason"]))
        if entry.get("displaced") is not None:
            validate_worker(str(entry["displaced"]))
        if op_id in seen:
            raise TodoError(
                f"sessions log for {item_id!r} has a duplicate operation ID {op_id!r}",
                code=E_STATE,
            )
        seen.add(op_id)


def record_session(
    snapshot: Snapshot,
    item_id: str,
    *,
    actor: str,
    session_id: str,
    client: str | None,
    op: str,
    op_id: str,
    generation: str | None = None,
    now: datetime | None = None,
    reason: str | None = None,
    displaced: str | None = None,
) -> bool:
    """Append one session-history entry to a task.

    Returns ``True`` when appended, ``False`` when an entry for *op_id*
    is already present (publication retry — never a duplicate). Raises
    on unknown tasks or malformed attribution.
    """
    if item_id not in snapshot.index["items"]:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
    actor = validate_worker(actor)
    session_id = validate_session_id(session_id)
    if client is not None:
        client = validate_client_name(client)
    if op not in SESSION_OPS:
        raise TodoError(f"unknown session operation {op!r}", code=E_STATE)
    if not isinstance(op_id, str) or not OP_ID_RE.fullmatch(op_id):
        raise TodoError("session entry needs a 32-hex operation ID", code=E_STATE)
    if generation is not None and (
        not isinstance(generation, str) or not GENERATION_RE.fullmatch(generation)
    ):
        raise TodoError("session entry has an invalid generation", code=E_STATE)
    if reason is not None:
        reason = validate_takeover_reason(reason)
    if displaced is not None:
        displaced = validate_worker(displaced)
    detail = snapshot.details[item_id]
    log = detail.get("sessions")
    if log is None:
        log = []
        detail["sessions"] = log
    if not isinstance(log, list):
        raise TodoError(f"sessions log for {item_id!r} must be a list", code=E_STATE)
    if any(isinstance(entry, dict) and entry.get("op_id") == op_id for entry in log):
        return False
    moment = now or datetime.now(timezone.utc)
    entry: dict[str, Any] = {
        "actor": actor,
        "session_id": session_id,
        "op": op,
        "at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "op_id": op_id,
    }
    if client is not None:
        entry["client"] = client
    if generation is not None:
        entry["generation"] = generation
    if reason is not None:
        entry["reason"] = reason
    if displaced is not None:
        entry["displaced"] = displaced
    log.append(entry)
    validate_snapshot(snapshot.index, snapshot.details)
    return True


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise TodoError(f"invalid timestamp {raw!r}: expected UTC ISO-8601 Z", code=E_STATE) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def new_generation() -> str:
    return uuid4().hex


#: Canonical stored form for ``not_before``, matching claim expiry stamps.
NOT_BEFORE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
#: Accepted input grammar: RFC 3339 date-time with a required offset.
#: ``fromisoformat`` alone also accepts ISO week dates and basic formats.
RFC3339_INPUT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:\d{2})$"
)


def normalize_not_before(raw: Any, now: datetime | None = None) -> str:
    """Validate a caller-supplied hold time and return its stored form.

    Input must carry an explicit offset: a naive time is ambiguous across
    writers, so unlike :func:`parse_ts` it is rejected rather than read as
    UTC. A time already past is rejected as a likely typo; stored values are
    never re-checked against the clock, since every hold eventually passes.
    Fractional seconds round up so a hold never ends early.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TodoError("not_before must be an RFC 3339 timestamp", code=E_STATE)
    text = raw.strip()
    match = RFC3339_INPUT_RE.fullmatch(text)
    if match is None:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?", text):
            raise TodoError(f"not_before {raw!r} has no UTC offset; add Z or +hh:mm", code=E_STATE)
        raise TodoError(f"invalid not_before {raw!r}: expected RFC 3339 with Z or an offset", code=E_STATE)
    day, clock, fraction, offset = match.groups()
    offset = "+00:00" if offset in ("Z", "z") else offset
    try:
        # Python 3.10's fromisoformat rejects most fraction lengths, so the
        # fraction is handled here rather than parsed.
        moment = datetime.fromisoformat(f"{day}T{clock}{offset}")
    except ValueError as exc:
        raise TodoError(f"invalid not_before {raw!r}: expected RFC 3339 with Z or an offset", code=E_STATE) from exc
    moment = moment.astimezone(timezone.utc)
    if fraction and fraction.strip("0"):
        moment += timedelta(seconds=1)
    if moment <= (now or datetime.now(timezone.utc)):
        raise TodoError(f"not_before {raw!r} is not in the future", code=E_STATE)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def hold_until(item_id: str, snapshot: Snapshot, now: datetime | None = None) -> str | None:
    """Return a task's hold time while it is still in the future, whatever its status."""
    if item_id not in snapshot.index["items"]:
        return None
    raw = snapshot.details.get(item_id, {}).get("not_before")
    if not isinstance(raw, str):
        return None
    if parse_ts(raw) <= (now or datetime.now(timezone.utc)):
        return None
    return raw


def waiting_until(item_id: str, snapshot: Snapshot, now: datetime | None = None) -> str | None:
    """Return the hold time while an open or blocked task is still waiting.

    Blocked tasks count so that parking a held task cannot unlock ``take``.
    """
    entry = snapshot.index["items"].get(item_id)
    if entry is None or entry["status"] not in ("open", "blocked"):
        return None
    return hold_until(item_id, snapshot, now)


def claim_is_live(claim: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not claim:
        return False
    try:
        expires = parse_ts(str(claim.get("expires_at", "")))
    except TodoError:
        return False
    moment = now or datetime.now(timezone.utc)
    return expires > moment


def validate_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id:
        raise TodoError(f"invalid task ID {item_id!r}: must be a non-empty string", code=E_STATE)
    if len(item_id) > MAX_ID_LEN or not ID_RE.fullmatch(item_id):
        raise TodoError(
            f"invalid task ID {item_id!r}: use 1-{MAX_ID_LEN} chars of "
            "[a-z0-9-], starting and ending alphanumeric",
            code=E_STATE,
        )


def validate_worker(worker: str) -> str:
    if not isinstance(worker, str) or not worker.strip():
        raise TodoError("worker identity must be a non-empty string", code=E_STATE)
    cleaned = worker.strip()
    if len(cleaned) > MAX_WORKER_LEN:
        raise TodoError(f"worker identity exceeds {MAX_WORKER_LEN} chars", code=E_STATE)
    # Worker identities land in commit trailers, where a newline would forge
    # protocol lines. Single line, no control characters — and no Unicode
    # line/paragraph separators, which pass the control check but still
    # split lines for trailer parsing.
    if _has_control_or_break(cleaned):
        raise TodoError("worker identity must be a single line without control characters", code=E_STATE)
    return cleaned


def validate_session_id(session: str) -> str:
    """Validate a session identity: one bounded line, no control characters.

    Session IDs land in session-history entries and commit trailers, where
    a newline would forge protocol lines — the same rule as worker
    identities, with the MCP ``--session`` bound (256 chars).
    """
    if not isinstance(session, str) or not session.strip():
        raise TodoError("session identity must be a non-empty string", code=E_STATE)
    cleaned = session.strip()
    if len(cleaned) > MAX_SESSION_LEN:
        raise TodoError(f"session identity exceeds {MAX_SESSION_LEN} chars", code=E_STATE)
    if _has_control_or_break(cleaned):
        raise TodoError("session identity must be a single line without control characters", code=E_STATE)
    return cleaned


def validate_takeover_reason(reason: str) -> str:
    """Validate a takeover reason: one bounded line, never empty.

    Reasons are durable Git state, returned in session reads, and quoted
    into commit summaries — so they must not carry newlines, controls,
    Unicode line separators (trailer forgery), or secrets (guidance, not
    enforcement: never put credentials in a reason).
    """
    if not isinstance(reason, str) or not reason.strip():
        raise TodoError("takeover reason must be a non-empty string", code=E_STATE)
    cleaned = reason.strip()
    if len(cleaned) > MAX_REASON_LEN:
        raise TodoError(f"takeover reason exceeds {MAX_REASON_LEN} chars", code=E_STATE)
    if any(ord(char) < 32 for char in cleaned) or _has_forbidden_breaks(cleaned):
        raise TodoError("takeover reason must be a single line without control characters", code=E_STATE)
    return cleaned


def validate_client_name(client: str) -> str:
    """Validate a client (harness) label: one bounded line, no controls."""
    if not isinstance(client, str) or not client.strip():
        raise TodoError("client name must be a non-empty string", code=E_STATE)
    cleaned = client.strip()
    if len(cleaned) > MAX_CLIENT_LEN:
        raise TodoError(f"client name exceeds {MAX_CLIENT_LEN} chars", code=E_STATE)
    if _has_control_or_break(cleaned):
        raise TodoError("client name must be a single line without control characters", code=E_STATE)
    return cleaned


def validate_deferral_summary(summary: str) -> str:
    """Validate a deferral summary: bounded, never empty, single line.

    Summaries name the deferred work in list rows and commit lines, so
    they share the worker-identity single-line rule.
    """
    if not isinstance(summary, str) or not summary.strip():
        raise TodoError("deferral summary must be a non-empty string", code=E_STATE)
    cleaned = summary.strip()
    if len(cleaned) > MAX_DEFERRAL_SUMMARY_LEN:
        raise TodoError(f"deferral summary exceeds {MAX_DEFERRAL_SUMMARY_LEN} chars", code=E_STATE)
    if any(ord(char) < 32 for char in cleaned) or _has_forbidden_breaks(cleaned):
        raise TodoError("deferral summary must be a single line without control characters", code=E_STATE)
    return cleaned


def _validate_reason_text(reason: str, label: str) -> str:
    """Shared rules for multi-line reasons: bounded, never empty, no control
    characters other than newline/tab, no Unicode line separators."""
    if not isinstance(reason, str) or not reason.strip():
        raise TodoError(f"{label} must be a non-empty string", code=E_STATE)
    cleaned = reason.strip()
    if len(cleaned) > MAX_DEFERRAL_REASON_LEN:
        raise TodoError(f"{label} exceeds {MAX_DEFERRAL_REASON_LEN} chars", code=E_STATE)
    if any(char in FORBIDDEN_BREAKS for char in cleaned):
        raise TodoError(f"{label} must not contain Unicode line separators", code=E_STATE)
    if any(ord(char) < 32 and char not in ("\n", "\t") for char in cleaned):
        raise TodoError(f"{label} must not contain control characters", code=E_STATE)
    return cleaned


def validate_deferral_reason(reason: str) -> str:
    """Validate a deferral reason: bounded, never empty, multi-line allowed.

    Reasons justify deferral and dismissal in detail views, so bodies may
    span lines; control characters other than newline/tab are rejected.
    """
    return _validate_reason_text(reason, "deferral reason")


def validate_drop_reason(reason: str) -> str:
    """Validate the reason recorded when a task is dropped; same rules as a
    deferral reason."""
    return _validate_reason_text(reason, "drop reason")


def empty_index() -> dict[str, Any]:
    return {"schema_version": BOOTSTRAP_SCHEMA_VERSION, "items": {}, "batches": {}}


#: The exact index-entry schema. Anything else is rejected so detail files
#: can never shadow index-owned fields and index entries can never shadow
#: detail-owned fields: authority is exact on both sides.
ENTRY_KEYS = frozenset({"title", "priority", "status", "claim", "needs"})


def _check_entry_shape(item_id: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise TodoError(f"index entry {item_id!r} must be an object", code=E_STATE)
    unknown = set(entry) - ENTRY_KEYS
    if unknown:
        raise TodoError(
            f"index entry {item_id!r} has unexpected keys: {', '.join(sorted(unknown))}",
            code=E_STATE,
        )
    for key in ("title", "priority", "status"):
        if key not in entry:
            raise TodoError(f"index entry {item_id!r} is missing {key!r}", code=E_STATE)
    title = entry["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LEN:
        raise TodoError(
            f"index entry {item_id!r} has an invalid title: 1-{MAX_TITLE_LEN} chars required",
            code=E_STATE,
        )
    if entry["priority"] not in PRIORITIES:
        raise TodoError(f"index entry {item_id!r} has unknown priority {entry['priority']!r}", code=E_STATE)
    if entry["status"] not in STATUSES:
        raise TodoError(f"index entry {item_id!r} has unknown status {entry['status']!r}", code=E_STATE)
    needs = entry.get("needs", [])
    if not isinstance(needs, list) or any(not isinstance(dep, str) for dep in needs):
        raise TodoError(f"index entry {item_id!r} has invalid needs: expected a list of IDs", code=E_STATE)
    claim = entry.get("claim")
    if claim is not None:
        if not isinstance(claim, dict):
            raise TodoError(f"index entry {item_id!r} has an invalid claim", code=E_STATE)
        for key in ("worker", "expires_at", "generation"):
            if key not in claim:
                raise TodoError(f"index entry {item_id!r} claim is missing {key!r}", code=E_STATE)
        validate_worker(str(claim["worker"]))
        parse_ts(str(claim["expires_at"]))
        generation = str(claim["generation"])
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise TodoError(f"index entry {item_id!r} claim has an invalid generation", code=E_STATE)
        renewals = claim.get("renewals", 0)
        if not isinstance(renewals, int) or isinstance(renewals, bool) or renewals < 0:
            raise TodoError(f"index entry {item_id!r} claim has an invalid renewals counter", code=E_STATE)
    return entry


def _validate_revision(value: Any, label: str) -> str:
    revision = str(value) if isinstance(value, str) else ""
    if not REVISION_RE.fullmatch(revision):
        raise TodoError(f"{label} must be a full lowercase Git revision", code=E_STATE)
    return revision


def _validate_deferral(item_id: str, pos: int, row: Any) -> dict[str, Any]:
    """Validate one detail-owned deferral row on its source task."""
    if not isinstance(row, dict):
        raise TodoError(f"deferral {pos} for {item_id!r} must be an object", code=E_STATE)
    number = row.get("id")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise TodoError(f"deferral {pos} for {item_id!r} has an invalid id", code=E_STATE)
    if row.get("from_item") != item_id:
        raise TodoError(f"deferral {number} for {item_id!r} names a foreign source", code=E_STATE)
    summary = row.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_DEFERRAL_SUMMARY_LEN:
        raise TodoError(f"deferral {number} for {item_id!r} has an invalid summary", code=E_STATE)
    if any(ord(char) < 32 for char in summary) or _has_forbidden_breaks(summary):
        raise TodoError(f"deferral {number} for {item_id!r} has an invalid summary", code=E_STATE)
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_DEFERRAL_REASON_LEN:
        raise TodoError(f"deferral {number} for {item_id!r} has an invalid reason", code=E_STATE)
    resolution = row.get("resolution", "open")
    if resolution not in DEFERRAL_RESOLUTIONS:
        raise TodoError(f"deferral {number} for {item_id!r} has an unknown resolution", code=E_STATE)
    resolved_item = row.get("resolved_item")
    if resolved_item is not None and not isinstance(resolved_item, str):
        raise TodoError(f"deferral {number} for {item_id!r} has an invalid resolved_item", code=E_STATE)
    resolved_reason = row.get("resolved_reason")
    if resolved_reason is not None and not isinstance(resolved_reason, str):
        raise TodoError(f"deferral {number} for {item_id!r} has an invalid resolved_reason", code=E_STATE)
    if resolution == "open":
        if resolved_item is not None or resolved_reason is not None:
            raise TodoError(f"open deferral {number} for {item_id!r} must not carry a resolution", code=E_STATE)
    elif resolution == "promoted":
        if not resolved_item:
            raise TodoError(f"promoted deferral {number} for {item_id!r} needs its successor item", code=E_STATE)
        if resolved_reason is not None:
            raise TodoError(f"promoted deferral {number} for {item_id!r} must not carry a dismissal reason", code=E_STATE)
    else:  # dismissed
        if resolved_item is not None:
            raise TodoError(f"dismissed deferral {number} for {item_id!r} must not carry a successor item", code=E_STATE)
        if not isinstance(resolved_reason, str) or not resolved_reason.strip():
            raise TodoError(f"dismissed deferral {number} for {item_id!r} needs a reason", code=E_STATE)
    created_at = row.get("created_at")
    if not isinstance(created_at, str):
        raise TodoError(f"deferral {number} for {item_id!r} needs a creation timestamp", code=E_STATE)
    parse_ts(created_at)
    resolved_at = row.get("resolved_at")
    if resolved_at is not None:
        if not isinstance(resolved_at, str):
            raise TodoError(f"deferral {number} for {item_id!r} has an invalid resolution timestamp", code=E_STATE)
        parse_ts(resolved_at)
    elif resolution != "open":
        raise TodoError(f"resolved deferral {number} for {item_id!r} needs a resolution timestamp", code=E_STATE)
    return row


def _deferrals_of(detail: dict[str, Any]) -> list[dict[str, Any]]:
    rows = detail.get("deferrals", [])
    if not isinstance(rows, list):
        raise TodoError("deferrals must be a list", code=E_STATE)
    return rows


def open_deferrals(snapshot: Snapshot, item_id: str) -> list[dict[str, Any]]:
    """Return the open deferral rows recorded on one task."""
    detail = snapshot.details.get(item_id, {})
    return [row for row in _deferrals_of(detail) if isinstance(row, dict) and row.get("resolution", "open") == "open"]


def scope_digest(scope: dict[str, list[str]]) -> str:
    """Return the canonical digest for an ordered member scope manifest."""
    blob = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def batch_schema_of(batch: dict[str, Any]) -> int:
    value = batch.get("batch_schema", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _validate_develop_ref(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(DEVELOP_REF_PREFIX)
        or len(value) == len(DEVELOP_REF_PREFIX)
        or len(value) > 200
        or ".." in value
        or any(ord(ch) < 33 or ch in "~^:?*[\\" for ch in value)
    ):
        raise TodoError(
            f"develop_ref must be a remote-tracking ref under {DEVELOP_REF_PREFIX}",
            code=E_STATE,
        )
    return value


def _validate_path_list(label: str, value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(path, str) or not path for path in value)
        or value != sorted(set(value))
    ):
        raise TodoError(f"{label} must be a sorted list of unique paths", code=E_STATE)
    return value


def _validate_batch_v2_fields(batch_id: str, batch: dict[str, Any]) -> None:
    for key in ("owner_epoch", "integration_log", "recoveries", "final_pr_history", "integration_head"):
        if key not in batch:
            raise TodoError(f"batch {batch_id!r} is missing {key!r}", code=E_STATE)
    epoch = batch["owner_epoch"]
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise TodoError(f"batch {batch_id!r} has an invalid owner_epoch", code=E_STATE)
    if "develop_ref" in batch:
        _validate_develop_ref(batch["develop_ref"])
    log = batch["integration_log"]
    if not isinstance(log, list) or not log:
        raise TodoError(f"batch {batch_id!r} has an invalid integration log", code=E_STATE)
    for seq, entry in enumerate(log):
        if not isinstance(entry, dict) or entry.get("seq") != seq:
            raise TodoError(f"batch {batch_id!r} integration log entry {seq} is malformed", code=E_STATE)
        _validate_revision(entry.get("head"), f"integration log head {seq}")
        kind = entry.get("kind")
        if kind not in ("start", "prepare", "reprepare", "fix", "develop-merge"):
            raise TodoError(f"batch {batch_id!r} integration log entry {seq} has an invalid kind", code=E_STATE)
        if (seq == 0) != (kind == "start"):
            raise TodoError(f"batch {batch_id!r} integration log must start with exactly one start entry", code=E_STATE)
        if seq:
            _validate_revision(entry.get("previous_head"), f"integration log previous head {seq}")
            if entry["previous_head"] != log[seq - 1]["head"]:
                raise TodoError(f"batch {batch_id!r} integration log is not a chain at {seq}", code=E_STATE)
            if (kind == "reprepare") != (entry["head"] == entry["previous_head"]):
                raise TodoError(f"batch {batch_id!r} integration log entry {seq} disagrees with its kind", code=E_STATE)
        if kind in ("fix", "develop-merge"):
            _validate_path_list("advance changed_files", entry.get("changed_files"))
        if kind == "fix":
            requires = entry.get("requires_acceptance")
            if not isinstance(requires, list) or any(item not in batch["members"] for item in requires):
                raise TodoError(f"batch {batch_id!r} fix entry {seq} has invalid requires_acceptance", code=E_STATE)
        if kind == "develop-merge":
            _validate_revision(entry.get("merged_parent"), f"merged_parent {seq}")
        if kind in ("prepare", "reprepare") and entry.get("member") not in batch["members"]:
            raise TodoError(f"batch {batch_id!r} integration log entry {seq} names a foreign member", code=E_STATE)
    if log[0]["head"] != batch["start_head"] or log[-1]["head"] != batch["integration_head"]:
        raise TodoError(f"batch {batch_id!r} integration log disagrees with its registered heads", code=E_STATE)
    for member, evidence in batch.get("integrated_members", {}).items():
        seq = evidence.get("integration_seq")
        if not isinstance(seq, int) or not 0 < seq < len(log) or log[seq]["head"] != evidence["integration_head"]:
            raise TodoError(f"batch {batch_id!r} integration evidence for {member!r} disagrees with the log", code=E_STATE)
    for key in ("recoveries", "final_pr_history"):
        if not isinstance(batch[key], list) or any(not isinstance(row, dict) for row in batch[key]):
            raise TodoError(f"batch {batch_id!r} has invalid {key}", code=E_STATE)


def _validate_batch_record(batch_id: str, batch: Any) -> dict[str, Any]:
    if not isinstance(batch, dict):
        raise TodoError(f"batch {batch_id!r} must be an object", code=E_STATE)
    required = (
        "project_id", "repository", "owner", "owner_generation", "integration_branch",
        "integration_worktree", "start_head", "members", "scope", "scope_hash",
        "delivery_boundary", "terminal_outcome",
    )
    for key in required:
        if key not in batch:
            raise TodoError(f"batch {batch_id!r} is missing {key!r}", code=E_STATE)
    validate_id(batch_id)
    for key in ("project_id", "repository", "owner", "integration_branch", "delivery_boundary", "terminal_outcome"):
        if not isinstance(batch[key], str) or not batch[key].strip() or any(ord(ch) < 32 for ch in batch[key]):
            raise TodoError(f"batch {batch_id!r} has invalid {key}", code=E_STATE)
    generation = batch["owner_generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise TodoError(f"batch {batch_id!r} has an invalid owner generation", code=E_STATE)
    worktree = batch["integration_worktree"]
    if not isinstance(worktree, str) or not os.path.isabs(worktree):
        raise TodoError(f"batch {batch_id!r} requires an absolute integration_worktree", code=E_STATE)
    _validate_revision(batch["start_head"], "batch start_head")
    members = batch["members"]
    if not isinstance(members, list) or not members or any(not isinstance(member, str) for member in members):
        raise TodoError(f"batch {batch_id!r} requires ordered member IDs", code=E_STATE)
    if len(set(members)) != len(members):
        raise TodoError(f"batch {batch_id!r} has duplicate members", code=E_STATE)
    for member in members:
        validate_id(member)
    scope = batch["scope"]
    if not isinstance(scope, dict) or set(scope) != set(members):
        raise TodoError(f"batch {batch_id!r} scope must cover exactly its members", code=E_STATE)
    for member in members:
        paths = scope[member]
        if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path for path in paths):
            raise TodoError(f"batch {batch_id!r} has invalid scope for {member!r}", code=E_STATE)
    digest = batch["scope_hash"]
    if not isinstance(digest, str) or not SCOPE_HASH_RE.fullmatch(digest) or digest != scope_digest(scope):
        raise TodoError(f"batch {batch_id!r} scope_hash does not match its frozen scope", code=E_STATE)
    if "integration_head" in batch:
        _validate_revision(batch["integration_head"], "batch integration_head")
    lifecycle = batch.get("lifecycle", "active")
    if lifecycle not in ("active", "aborted", "completed"):
        raise TodoError(f"batch {batch_id!r} has invalid lifecycle", code=E_STATE)
    accepted_members = batch.get("accepted_members", {})
    if not isinstance(accepted_members, dict) or set(accepted_members) - set(members):
        raise TodoError(f"batch {batch_id!r} has invalid accepted member heads", code=E_STATE)
    for member, head in accepted_members.items():
        _validate_revision(head, f"batch accepted head for {member!r}")
    integrated_members = batch.get("integrated_members", {})
    if not isinstance(integrated_members, dict) or set(integrated_members) - set(members):
        raise TodoError(f"batch {batch_id!r} has invalid integrated member evidence", code=E_STATE)
    for member, evidence in integrated_members.items():
        if not isinstance(evidence, dict):
            raise TodoError(f"batch {batch_id!r} has invalid integration evidence for {member!r}", code=E_STATE)
        _validate_revision(evidence.get("accepted_head"), f"integrated accepted head for {member!r}")
        _validate_revision(evidence.get("integration_head"), f"integrated head for {member!r}")
        if "changed_files" in evidence:
            _validate_path_list(f"integrated changed files for {member!r}", evidence["changed_files"])
    schema = batch_schema_of(batch)
    if schema not in (1, BATCH_SCHEMA):
        raise TodoError(f"batch {batch_id!r} has an unknown batch_schema", code=E_SCHEMA)
    if schema == BATCH_SCHEMA:
        _validate_batch_v2_fields(batch_id, batch)
    final_pr = batch.get("final_pr")
    if final_pr is not None:
        if not isinstance(final_pr, dict):
            raise TodoError(f"batch {batch_id!r} final_pr must be an object", code=E_STATE)
        if schema == BATCH_SCHEMA and (
            not isinstance(final_pr.get("repository"), str) or not final_pr["repository"].strip()
        ):
            raise TodoError(f"batch {batch_id!r} final_pr has no repository", code=E_STATE)
        if not isinstance(final_pr.get("number"), int) or final_pr["number"] <= 0:
            raise TodoError(f"batch {batch_id!r} final_pr has an invalid number", code=E_STATE)
        if not isinstance(final_pr.get("node_id"), str) or not final_pr["node_id"].strip():
            raise TodoError(f"batch {batch_id!r} final_pr has no node_id", code=E_STATE)
        _validate_revision(final_pr.get("head"), "final_pr head")
    return batch


def _validate_prepared_receipt(item_id: str, prepared: Any) -> dict[str, Any]:
    if not isinstance(prepared, dict):
        raise TodoError(f"prepared receipt for {item_id!r} must be an object", code=E_STATE)
    schema = prepared.get("schema")
    if schema not in (PREPARED_RECEIPT_SCHEMA, LEGACY_RECEIPT_SCHEMA):
        raise TodoError(f"prepared receipt for {item_id!r} has an unknown schema", code=E_SCHEMA)
    v2 = schema == PREPARED_RECEIPT_SCHEMA
    required = [
        "batch_id", "member_id", "source_worktree", "source_revision",
        "source_base", "accepted_head", "integration_head", "scope_hash", "changed_files", "verification",
    ]
    required += ["owner_epoch", "integration_seq", "overlaps"] if v2 else ["owner_generation"]
    for key in required:
        if key not in prepared:
            raise TodoError(f"prepared receipt for {item_id!r} is missing {key!r}", code=E_STATE)
    validate_id(prepared["batch_id"])
    validate_id(prepared["member_id"])
    if v2:
        epoch = prepared["owner_epoch"]
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise TodoError(f"prepared receipt for {item_id!r} has an invalid owner epoch", code=E_STATE)
        seq = prepared["integration_seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise TodoError(f"prepared receipt for {item_id!r} has an invalid integration_seq", code=E_STATE)
        overlaps = prepared["overlaps"]
        if not isinstance(overlaps, list):
            raise TodoError(f"prepared receipt for {item_id!r} has invalid overlaps", code=E_STATE)
        for row in overlaps:
            if (
                not isinstance(row, dict)
                or row.get("kind") not in ("member", "fix")
                or not row.get("paths")
                or (row["kind"] == "member" and not isinstance(row.get("member"), str))
                or (row["kind"] == "fix" and not isinstance(row.get("seq"), int))
            ):
                raise TodoError(f"prepared receipt for {item_id!r} has a malformed overlap", code=E_STATE)
            _validate_path_list("overlap paths", row["paths"])
    else:
        generation = prepared["owner_generation"]
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise TodoError(f"prepared receipt for {item_id!r} has an invalid owner generation", code=E_STATE)
    worktree = prepared["source_worktree"]
    if not isinstance(worktree, str) or not worktree or not os.path.isabs(worktree):
        raise TodoError(f"prepared receipt for {item_id!r} requires an absolute source_worktree", code=E_STATE)
    revision = _validate_revision(prepared["source_revision"], "source_revision")
    _validate_revision(prepared["source_base"], "source_base")
    accepted_head = _validate_revision(prepared["accepted_head"], "accepted_head")
    _validate_revision(prepared["integration_head"], "integration_head")
    if accepted_head != revision:
        raise TodoError(f"prepared receipt for {item_id!r} does not bind the accepted worker revision", code=E_STATE)
    scope_hash = prepared["scope_hash"]
    if not isinstance(scope_hash, str) or not SCOPE_HASH_RE.fullmatch(scope_hash):
        raise TodoError(f"prepared receipt for {item_id!r} has an invalid scope_hash", code=E_STATE)
    changed_files = prepared["changed_files"]
    if not isinstance(changed_files, list) or any(not isinstance(path, str) or not path for path in changed_files):
        raise TodoError(f"prepared receipt for {item_id!r} has invalid changed files", code=E_STATE)
    if changed_files != sorted(set(changed_files)):
        raise TodoError(f"prepared receipt for {item_id!r} changed files are not canonical", code=E_STATE)
    verification = prepared["verification"]
    if not isinstance(verification, dict):
        raise TodoError(f"prepared receipt for {item_id!r} has invalid verification evidence", code=E_STATE)
    if verification.get("status") != "passed":
        raise TodoError(f"prepared receipt for {item_id!r} requires passed verification", code=E_STATE)
    if verification.get("revision") != revision:
        raise TodoError(f"prepared receipt for {item_id!r} verification drifted from source_revision", code=E_STATE)
    if verification.get("clean") is not True:
        raise TodoError(f"prepared receipt for {item_id!r} requires a clean source checkout", code=E_STATE)
    suite = verification.get("suite")
    command = verification.get("command")
    if not isinstance(suite, str) or not suite.strip():
        raise TodoError(f"prepared receipt for {item_id!r} requires a bounded verification suite", code=E_STATE)
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
        raise TodoError(f"prepared receipt for {item_id!r} requires a non-empty verification command", code=E_STATE)
    return prepared


def _validate_final_evidence(item_id: str, evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict) or evidence.get("status") != "passed":
        raise TodoError(f"prepared task {item_id!r} requires passed final-tree evidence", code=E_FINAL_EVIDENCE)
    for key in (
        "batch_id", "project_id", "repository", "tree_worktree", "tree_revision", "integration_branch",
        "integration_head", "scope_hash", "suite",
    ):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            raise TodoError(f"final evidence for {item_id!r} requires {key}", code=E_FINAL_EVIDENCE)
    if not os.path.isabs(evidence["tree_worktree"]):
        raise TodoError(f"final evidence for {item_id!r} requires an absolute tree_worktree", code=E_FINAL_EVIDENCE)
    _validate_revision(evidence["tree_revision"], "tree_revision")
    _validate_revision(evidence["integration_head"], "integration_head")
    if evidence["tree_revision"] != evidence["integration_head"]:
        raise TodoError(f"final evidence for {item_id!r} has inconsistent tree/head", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence.get("member_heads"), dict) or not isinstance(evidence.get("member_ranges"), dict):
        raise TodoError(f"final evidence for {item_id!r} has invalid member mappings", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence.get("changed_files"), list) or any(
        not isinstance(path, str) for path in evidence.get("changed_files", [])
    ):
        raise TodoError(f"final evidence for {item_id!r} has invalid changed_files", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence.get("final_pr"), dict):
        raise TodoError(f"final evidence for {item_id!r} has no final PR identity", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence["final_pr"].get("number"), int) or not isinstance(evidence["final_pr"].get("node_id"), str):
        raise TodoError(f"final evidence for {item_id!r} has an invalid final PR identity", code=E_FINAL_EVIDENCE)
    _validate_revision(evidence["final_pr"].get("head"), "final_pr head")
    if evidence.get("clean") is not True:
        raise TodoError(f"final evidence for {item_id!r} requires a clean final tree", code=E_FINAL_EVIDENCE)
    return evidence


def _v2_receipts(snapshot: Snapshot, batch_id: str, batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for member in batch["members"]:
        receipt = snapshot.details.get(member, {}).get("prepared")
        if isinstance(receipt, dict) and receipt.get("batch_id") == batch_id:
            out[member] = receipt
    return out


def acceptance_required(snapshot: Snapshot, batch_id: str) -> dict[str, list[dict[str, Any]]]:
    """Members needing current-acceptance evidence at final, with reasons.

    Derived on every call from receipts and the integration log; never stored.
    """
    batch = snapshot.index["batches"][batch_id]
    if batch_schema_of(batch) != BATCH_SCHEMA:
        return {}
    receipts = _v2_receipts(snapshot, batch_id, batch)
    required: dict[str, list[dict[str, Any]]] = {}
    for member, receipt in receipts.items():
        if receipt.get("schema") != PREPARED_RECEIPT_SCHEMA:
            continue
        for overlap in receipt.get("overlaps", []):
            if overlap["kind"] == "member":
                required.setdefault(overlap["member"], []).append(
                    {"kind": "overlap", "by": member, "paths": overlap["paths"]}
                )
            else:
                required.setdefault(member, []).append(
                    {"kind": "fix-overlap", "seq": overlap["seq"], "paths": overlap["paths"]}
                )
    for entry in batch["integration_log"]:
        if entry["kind"] != "fix":
            continue
        for member in entry["requires_acceptance"]:
            touched = sorted(set(entry["changed_files"]) & set(receipts.get(member, {}).get("changed_files", [])))
            required.setdefault(member, []).append({"kind": "fix", "seq": entry["seq"], "paths": touched})
    return required


def excluded_paths(snapshot: Snapshot, batch_id: str) -> set[str]:
    """Develop-merge paths exempt from final scope accounting.

    A path that a member receipt or a fix entry also touched stays subject to
    scope, so a develop merge never hides a member or fix change.
    """
    batch = snapshot.index["batches"][batch_id]
    if batch_schema_of(batch) != BATCH_SCHEMA:
        return set()
    develop: set[str] = set()
    owned: set[str] = set()
    for entry in batch["integration_log"]:
        if entry["kind"] == "develop-merge":
            develop.update(entry["changed_files"])
        elif entry["kind"] == "fix":
            owned.update(entry["changed_files"])
    for receipt in _v2_receipts(snapshot, batch_id, batch).values():
        owned.update(receipt.get("changed_files", []))
    return develop - owned


def _validate_current_acceptance(
    snapshot: Snapshot, item_id: str, batch_id: str, batch: dict[str, Any], evidence: dict[str, Any],
) -> None:
    required = acceptance_required(snapshot, batch_id)
    acceptance = evidence.get("current_acceptance", {})
    if not isinstance(acceptance, dict):
        raise TodoError(
            f"final evidence for {item_id!r} has invalid current_acceptance", code=E_FINAL_EVIDENCE,
            recovery=["pass current_acceptance as an object keyed by member id"],
        )
    receipts = _v2_receipts(snapshot, batch_id, batch)
    for member, reasons in sorted(required.items()):
        if member not in acceptance:
            why = ", ".join(sorted({row["kind"] for row in reasons}))
            raise TodoError(
                f"final evidence for {item_id!r} lacks current acceptance for {member!r} ({why})",
                code=E_FINAL_EVIDENCE,
                recovery=[f"rerun {member!r}'s own verification at the final integration head and pass it in current_acceptance"],
            )
    for member, row in acceptance.items():
        receipt = receipts.get(member)
        if receipt is None:
            raise TodoError(
                f"current acceptance names unknown member {member!r}", code=E_FINAL_EVIDENCE,
                recovery=["pass current_acceptance only for members with a prepared receipt in the batch"],
            )
        verification = receipt["verification"]
        if (
            not isinstance(row, dict)
            or row.get("status") != "passed"
            or row.get("clean") is not True
            or row.get("revision") != evidence["integration_head"]
            or row.get("worktree") != batch["integration_worktree"]
            or row.get("suite") != verification["suite"]
            or row.get("command") != verification["command"]
        ):
            raise TodoError(
                f"current acceptance for {member!r} is not its own passed verification at the final head",
                code=E_FINAL_EVIDENCE,
                recovery=[f"rerun {member!r}'s own verification in the integration worktree at the final head"],
            )


def _validate_batch_final_evidence(snapshot: Snapshot, item_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Validate final evidence against every prepared member and its registry."""
    detail = snapshot.details[item_id]
    batch_meta = detail.get("batch") or {}
    batch_id = batch_meta.get("batch_id")
    batch = snapshot.index.get("batches", {}).get(batch_id)
    if batch is None or evidence.get("batch_id") != batch_id:
        raise TodoError(f"final evidence for {item_id!r} names a foreign batch", code=E_FINAL_EVIDENCE)
    if evidence.get("integration_branch") != batch["integration_branch"]:
        raise TodoError(f"final evidence for {item_id!r} names a foreign integration branch", code=E_FINAL_EVIDENCE)
    if evidence.get("scope_hash") != batch["scope_hash"]:
        raise TodoError(f"final evidence for {item_id!r} has a foreign scope", code=E_FINAL_EVIDENCE)
    if evidence.get("project_id") != batch["project_id"] or evidence.get("repository") != batch["repository"]:
        raise TodoError(f"final evidence for {item_id!r} names a foreign repository", code=E_FINAL_EVIDENCE)
    if evidence.get("integration_head") != batch.get("integration_head"):
        raise TodoError(f"final evidence for {item_id!r} is not at the registered integration head", code=E_FINAL_EVIDENCE)
    final_pr = batch.get("final_pr")
    if final_pr is None or evidence.get("final_pr") != final_pr:
        raise TodoError(f"final evidence for {item_id!r} is not bound to the declared final PR", code=E_FINAL_EVIDENCE)
    if final_pr["head"] != evidence["integration_head"]:
        raise TodoError(f"final evidence for {item_id!r} is not at the final PR head", code=E_FINAL_EVIDENCE)
    v2 = batch_schema_of(batch) == BATCH_SCHEMA
    members = batch["members"]
    heads = evidence["member_heads"]
    ranges = evidence["member_ranges"]
    if set(heads) != set(members) or set(ranges) != set(members):
        raise TodoError(f"final evidence for {item_id!r} does not map every declared member", code=E_FINAL_EVIDENCE)
    allowed: list[str] = []
    for member in members:
        member_detail = snapshot.details.get(member, {})
        receipt = member_detail.get("prepared")
        if not isinstance(receipt, dict) or receipt.get("batch_id") != batch_id:
            raise TodoError(f"member {member!r} lacks a valid prepared receipt", code=E_FINAL_EVIDENCE)
        if heads[member] != receipt["accepted_head"]:
            raise TodoError(f"member {member!r} final head does not match its prepared head", code=E_FINAL_EVIDENCE)
        member_range = ranges[member]
        if not isinstance(member_range, dict) or member_range.get("base") != receipt["source_base"] or member_range.get("head") != heads[member]:
            raise TodoError(f"member {member!r} range is not bound to its prepared receipt", code=E_FINAL_EVIDENCE)
        allowed.extend(batch["scope"][member])
    if v2:
        _validate_current_acceptance(snapshot, item_id, batch_id, batch, evidence)
    for path in evidence["changed_files"]:
        if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed):
            raise TodoError(f"final evidence includes unauthorized file {path!r}", code=E_FINAL_EVIDENCE)
    # The union check is intentionally independent of member_ranges: a caller
    # cannot hide an out-of-scope file by assigning it to a different member.
    union = [pattern for member in members for pattern in batch["scope"][member]]
    if any(not any(fnmatch.fnmatch(path, pattern) for pattern in union) for path in evidence["changed_files"]):
        raise TodoError(f"final evidence for {item_id!r} exceeds the frozen batch scope", code=E_FINAL_EVIDENCE)
    return evidence


def _validate_batch_detail(item_id: str, detail: dict[str, Any], needs: list[str]) -> None:
    batch = detail.get("batch")
    if batch is None:
        return
    if not isinstance(batch, dict):
        raise TodoError(f"batch metadata for {item_id!r} must be an object", code=E_STATE)
    for key in ("batch_id", "member_id", "implementation_dependencies"):
        if key not in batch:
            raise TodoError(f"batch metadata for {item_id!r} is missing {key!r}", code=E_STATE)
    validate_id(batch["batch_id"])
    validate_id(batch["member_id"])
    deps = batch["implementation_dependencies"]
    if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
        raise TodoError(f"batch metadata for {item_id!r} has invalid implementation dependencies", code=E_STATE)
    if len(set(deps)) != len(deps) or any(dep not in needs for dep in deps):
        raise TodoError(f"batch metadata for {item_id!r} must name unique task dependencies", code=E_STATE)
    if deps and batch["member_id"] in deps:
        raise TodoError(f"batch metadata for {item_id!r} cannot depend on itself", code=E_STATE)


def _check_cycles(items: dict[str, Any]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join([*trail, node])
            raise TodoError(f"dependency cycle detected: {cycle}", code=E_STATE)
        visiting.add(node)
        for dep in items[node].get("needs", []):
            visit(dep, [*trail, node])
        visiting.discard(node)
        visited.add(node)

    for item_id in items:
        visit(item_id, [])


def validate_snapshot(index: Any, details: dict[str, Any]) -> dict[str, Any]:
    """Validate index/detail correspondence. Returns the index on success."""
    if not isinstance(index, dict):
        raise TodoError("index.json must contain a JSON object", code=E_STATE)
    version = index.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise TodoError(
            f"unsupported index schema version {version!r}: this package reads {SUPPORTED_SCHEMA_VERSIONS}",
            code=E_SCHEMA,
        )
    items = index.get("items")
    if not isinstance(items, dict):
        raise TodoError("index.json items must be an object keyed by ID", code=E_STATE)
    batches = index.get("batches", {})
    if version >= 3:
        if not isinstance(batches, dict):
            raise TodoError("index.json batches must be an object keyed by batch ID", code=E_STATE)
        for batch_id, batch in batches.items():
            _validate_batch_record(batch_id, batch)
            if version < SCHEMA_VERSION and batch_schema_of(batch) != 1:
                raise TodoError("batch v2 records require index schema version 4", code=E_SCHEMA)
    elif batches not in ({}, None):
        raise TodoError("batch registry requires index schema version 3", code=E_SCHEMA)
    for item_id, entry in items.items():
        validate_id(item_id)
        _check_entry_shape(item_id, entry)
    for item_id, entry in items.items():
        for dep in entry.get("needs", []):
            validate_id(dep)
            if dep == item_id:
                raise TodoError(f"task {item_id!r} cannot depend on itself", code=E_STATE)
            if dep not in items:
                raise TodoError(f"task {item_id!r} needs unknown task {dep!r}", code=E_STATE)
        if entry["status"] in TERMINAL_STATUSES and entry.get("claim") is not None:
            raise TodoError(f"task {item_id!r} is {entry['status']} but still carries a claim", code=E_STATE)
        if entry["status"] in ("open", "blocked") and entry.get("claim") is not None:
            raise TodoError(
                f"task {item_id!r} is {entry['status']} but carries a claim; "
                "only active tasks hold claims",
                code=E_STATE,
            )
    _check_cycles(items)
    if not isinstance(details, dict):
        raise TodoError("details must be a mapping of ID to detail object", code=E_STATE)
    missing = [item_id for item_id in items if item_id not in details]
    if missing:
        raise TodoError(f"missing detail files for: {', '.join(sorted(missing))}", code=E_STATE)
    orphaned = [item_id for item_id in details if item_id not in items]
    if orphaned:
        raise TodoError(f"orphaned detail files without index entries: {', '.join(sorted(orphaned))}", code=E_STATE)
    for item_id, detail in details.items():
        if not isinstance(detail, dict):
            raise TodoError(f"detail for {item_id!r} must be a JSON object", code=E_STATE)
        copies = INDEX_OWNED_FIELDS.intersection(detail)
        if copies:
            raise TodoError(
                f"detail for {item_id!r} must not copy index-owned fields: {', '.join(sorted(copies))}",
                code=E_STATE,
            )
        for key in ("acceptance", "links"):
            if key in detail and (
                not isinstance(detail[key], list) or any(not isinstance(v, str) for v in detail[key])
            ):
                raise TodoError(f"detail for {item_id!r}: {key!r} must be a list of strings", code=E_STATE)
        for key in ("description", "context"):
            if key in detail and not isinstance(detail[key], str):
                raise TodoError(f"detail for {item_id!r}: {key!r} must be a string", code=E_STATE)
        if "prepared" in detail:
            if version < 3:
                raise TodoError("registered prepared receipts require index schema version 3", code=E_SCHEMA)
            _validate_prepared_receipt(item_id, detail["prepared"])
        if "batch" in detail and version < 2:
            raise TodoError("batch metadata requires index schema version 2", code=E_SCHEMA)
        if "final_evidence" in detail:
            if version < 3:
                raise TodoError("final evidence requires index schema version 3", code=E_SCHEMA)
            _validate_final_evidence(item_id, detail["final_evidence"])
        if "not_before" in detail:
            # Additive like sessions: older readers load and preserve the
            # key but do not enforce the hold.
            value = detail["not_before"]
            if not isinstance(value, str) or not NOT_BEFORE_RE.fullmatch(value):
                raise TodoError(f"detail for {item_id!r}: 'not_before' must be UTC ISO Z", code=E_STATE)
            parse_ts(value)
        if "drop_reason" in detail:
            # Additive like sessions: absence stays valid for items dropped
            # before reasons were recorded; older readers keep the key.
            value = detail["drop_reason"]
            if not isinstance(value, str) or not value.strip():
                raise TodoError(f"detail for {item_id!r}: 'drop_reason' must be a non-empty string", code=E_STATE)
        if "sessions" in detail:
            # Additive session history: absence stays valid for
            # pre-history items, presence must validate.
            _validate_sessions(item_id, detail["sessions"])
        if "deferrals" in detail:
            # Additive deferral log: absence stays valid, presence must be
            # a list of rows keyed by unique positive integer ids scoped
            # to the source task.
            rows = detail["deferrals"]
            if not isinstance(rows, list):
                raise TodoError(f"detail for {item_id!r}: 'deferrals' must be a list", code=E_STATE)
            seen: set[int] = set()
            for pos, row in enumerate(rows):
                checked = _validate_deferral(item_id, pos, row)
                if checked["id"] in seen:
                    raise TodoError(
                        f"detail for {item_id!r} has a duplicate deferral id {checked['id']!r}",
                        code=E_STATE,
                    )
                seen.add(checked["id"])
        _validate_batch_detail(item_id, detail, list(items[item_id].get("needs", [])))

    # A prepared receipt is useful only to an explicitly registered member of
    # the same batch.  Reject duplicate member identities and cross-batch
    # receipt/metadata pairs before they can affect readiness.
    members: dict[tuple[str, str], str] = {}
    for item_id, detail in details.items():
        batch = detail.get("batch")
        prepared = detail.get("prepared")
        if batch is not None:
            identity = (batch["batch_id"], batch["member_id"])
            previous = members.get(identity)
            if previous is not None and previous != item_id:
                raise TodoError(f"duplicate batch member {identity!r}: {previous!r} and {item_id!r}", code=E_STATE)
            members[identity] = item_id
        if prepared is not None:
            if version < 3:
                raise TodoError("prepared receipts require index schema version 3", code=E_SCHEMA)
            batch = detail.get("batch")
            if batch is None or (batch["batch_id"], batch["member_id"]) != (
                prepared["batch_id"], prepared["member_id"]
            ):
                raise TodoError(f"prepared receipt for {item_id!r} does not match batch metadata", code=E_STATE)
            record = batches.get(prepared["batch_id"])
            if record is None:
                raise TodoError(f"prepared receipt for {item_id!r} names an unknown batch", code=E_STATE)
            if prepared["member_id"] not in record["members"]:
                raise TodoError(f"prepared receipt for {item_id!r} names an unregistered member", code=E_STATE)
            if prepared["scope_hash"] != record["scope_hash"]:
                raise TodoError(f"prepared receipt for {item_id!r} has a foreign scope", code=E_STATE)
            _check_receipt_against_batch(item_id, prepared, record)
        batch = detail.get("batch")
        if batch is not None and version >= 3:
            record = batches.get(batch["batch_id"])
            if record is None:
                raise TodoError(f"item {item_id!r} names an unknown batch", code=E_STATE)
            if batch["member_id"] != item_id or batch["member_id"] not in record["members"]:
                raise TodoError(f"item {item_id!r} is not a declared member of its batch", code=E_STATE)
    return index


def _check_receipt_against_batch(item_id: str, prepared: dict[str, Any], record: dict[str, Any]) -> None:
    member = prepared["member_id"]
    v2_batch = batch_schema_of(record) == BATCH_SCHEMA
    v2_receipt = prepared["schema"] == PREPARED_RECEIPT_SCHEMA
    if v2_batch and not v2_receipt:
        raise TodoError(
            f"prepared receipt for {item_id!r} uses {LEGACY_RECEIPT_SCHEMA}, which batch v2 never reinterprets",
            code=E_SCHEMA,
        )
    if v2_receipt and not v2_batch:
        raise TodoError(f"prepared receipt for {item_id!r} is v2 under a legacy batch", code=E_STATE)
    if record.get("accepted_members", {}).get(member) != prepared["accepted_head"]:
        raise TodoError(f"prepared receipt for {item_id!r} is not the registered accepted head", code=E_STATE)
    integrated = record.get("integrated_members", {}).get(member)
    if not isinstance(integrated, dict) or integrated.get("accepted_head") != prepared["accepted_head"]:
        raise TodoError(f"prepared receipt for {item_id!r} lacks integration ancestry evidence", code=E_STATE)
    if v2_receipt:
        if prepared["owner_epoch"] > record["owner_epoch"]:
            raise TodoError(f"prepared receipt for {item_id!r} is from a future owner epoch", code=E_STATE)
        if (
            integrated.get("integration_head") != prepared["integration_head"]
            or integrated.get("integration_seq") != prepared["integration_seq"]
        ):
            raise TodoError(f"prepared receipt for {item_id!r} disagrees with its integration entry", code=E_STATE)
    elif prepared["owner_generation"] != record["owner_generation"]:
        raise TodoError(f"prepared receipt for {item_id!r} has a stale owner generation", code=E_CLAIM_STALE)


@dataclass
class Snapshot:
    index: dict[str, Any]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


def _read_state_file(path: Path, label: str) -> Any:
    # State checkouts are Git content, but a hostile or corrupted worktree
    # could smuggle a symlink here; never follow one out of the checkout.
    if path.is_symlink():
        raise TodoError(f"refusing to follow symlink at {label}", code=E_STATE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TodoError(f"missing {label} in {path.parent}: is this a state checkout?", code=E_STATE) from exc
    except json.JSONDecodeError as exc:
        raise TodoError(f"invalid {label}: {exc}", code=E_STATE) from exc


def assemble_snapshot(
    read_file: Callable[[str, str], Any],
    detail_names: Callable[[], list[str]],
) -> Snapshot:
    """Build and validate a snapshot from any state tree.

    ``read_file(relpath, label)`` returns one parsed JSON file or raises
    ``TodoError``; ``detail_names()`` lists the ``*.json`` names directly
    under ``items/``. A checkout on disk and a commit in a Git object store
    share this one assembly, so both accept exactly the same trees.
    """
    index = read_file(INDEX_FILENAME, INDEX_FILENAME)
    details: dict[str, dict[str, Any]] = {}
    if isinstance(index, dict) and isinstance(index.get("items"), dict):
        for item_id in index["items"]:
            try:
                validate_id(item_id)
            except TodoError:
                continue
            details[item_id] = read_file(f"{ITEMS_DIRNAME}/{item_id}.json", f"detail file for {item_id!r}")
        for name in sorted(detail_names()):
            if name[:-5] not in index["items"]:
                details[name[:-5]] = read_file(f"{ITEMS_DIRNAME}/{name}", f"detail file {name}")
    validate_snapshot(index, details)
    return Snapshot(index=index, details=details)


def load_snapshot(state_dir: str | Path) -> Snapshot:
    """Load and validate one local state directory."""
    root = Path(state_dir)
    items_dir = root / ITEMS_DIRNAME

    def read_file(rel: str, label: str) -> Any:
        if rel.startswith(f"{ITEMS_DIRNAME}/") and items_dir.is_symlink():
            raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
        return _read_state_file(root / rel, label)

    def detail_names() -> list[str]:
        if items_dir.is_symlink():
            raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
        return [path.name for path in items_dir.glob("*.json")] if items_dir.is_dir() else []

    return assemble_snapshot(read_file, detail_names)


def save_snapshot(state_dir: str | Path, snapshot: Snapshot) -> None:
    """Atomically persist a snapshot after validation.

    Each file is written to a temporary sibling and renamed, so a partial
    local write never leaves a half-updated file behind. Published state
    still comes only from accepted Git commits; this guards the scratch
    copy readers and writers share on one machine.
    """
    validate_snapshot(snapshot.index, snapshot.details)
    root = Path(state_dir)
    items_dir = root / ITEMS_DIRNAME
    items_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / INDEX_FILENAME, snapshot.index)
    for item_id, detail in snapshot.details.items():
        _atomic_write(items_dir / f"{item_id}.json", detail)
    wanted = {f"{item_id}.json" for item_id in snapshot.details}
    for path in items_dir.glob("*.json"):
        if path.name not in wanted:
            path.unlink()


def serialize_state(payload: Any) -> str:
    """The canonical text of one state file, as committed to the branch."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, payload: Any) -> None:
    text = serialize_state(payload)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def entry_brief(item_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": entry["title"],
        "priority": entry["priority"],
        "status": entry["status"],
    }


def is_ready(item_id: str, snapshot: Snapshot, now: datetime | None = None) -> bool:
    entry = snapshot.index["items"].get(item_id)
    if entry is None or entry["status"] != "open":
        return False
    if claim_is_live(entry.get("claim"), now):
        return False
    if waiting_until(item_id, snapshot, now) is not None:
        return False
    detail = snapshot.details.get(item_id, {})
    batch = detail.get("batch")
    implementation_deps = set(batch.get("implementation_dependencies", [])) if isinstance(batch, dict) else set()
    batch_record = snapshot.index.get("batches", {}).get(batch.get("batch_id")) if isinstance(batch, dict) else None
    for dep in entry.get("needs", []):
        dep_entry = snapshot.index["items"][dep]
        if dep_entry["status"] == "done":
            continue
        if dep not in implementation_deps:
            return False
        dep_detail = snapshot.details.get(dep, {})
        receipt = dep_detail.get("prepared")
        if (
            not isinstance(receipt, dict)
            or batch_record is None
            or receipt.get("batch_id") != batch.get("batch_id")
            or receipt.get("member_id") != dep
            or receipt.get("scope_hash") != batch_record.get("scope_hash")
            or batch_record.get("accepted_members", {}).get(dep) != receipt.get("accepted_head")
            or batch_record.get("integrated_members", {}).get(dep, {}).get("accepted_head") != receipt.get("accepted_head")
        ):
            return False
        if batch_schema_of(batch_record) == BATCH_SCHEMA:
            if receipt.get("schema") != PREPARED_RECEIPT_SCHEMA:
                return False
        elif (
            receipt.get("owner_generation") != batch_record.get("owner_generation")
            or batch_record.get("integrated_members", {}).get(dep, {}).get("integration_head")
            != batch_record.get("integration_head")
        ):
            return False
        if dep_detail.get("batch", {}).get("batch_id") != batch.get("batch_id"):
            return False
        if dep_entry.get("claim") is not None:
            return False
    return True


def downstream_unlocks(item_id: str, snapshot: Snapshot) -> int:
    return sum(
        1
        for other_id, entry in snapshot.index["items"].items()
        if other_id != item_id
        and item_id in entry.get("needs", [])
        and entry["status"] not in TERMINAL_STATUSES
    )


def order_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    item_id, entry = item
    return (PRIORITY_RANK[entry["priority"]], item_id)


def query_items(
    snapshot: Snapshot,
    *,
    status: str | None = None,
    priority: str | None = None,
    text: str | None = None,
    ready_only: bool = False,
    now: datetime | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    moment = now or datetime.now(timezone.utc)
    if status is not None and status not in STATUSES:
        raise TodoError(f"unknown status {status!r}", code=E_STATE)
    if priority is not None and priority not in PRIORITIES:
        raise TodoError(f"unknown priority {priority!r}", code=E_STATE)
    needle = text.lower() if text else None
    rows: list[tuple[str, dict[str, Any]]] = []
    for item_id, entry in snapshot.index["items"].items():
        if status is not None and entry["status"] != status:
            continue
        if priority is not None and entry["priority"] != priority:
            continue
        if needle and needle not in item_id.lower() and needle not in str(entry["title"]).lower():
            continue
        if ready_only and not is_ready(item_id, snapshot, moment):
            continue
        rows.append((item_id, entry))
    rows.sort(key=order_key)
    return rows


def _require_batch_schema(snapshot: Snapshot, what: str) -> None:
    if snapshot.index["schema_version"] < SCHEMA_VERSION:
        raise TodoError(
            f"{what} requires index schema version {SCHEMA_VERSION}; only register_batch with "
            "confirm_schema3_cutover=true performs the cutover",
            code=E_SCHEMA,
            recovery=["register the batch with confirm_schema3_cutover=true after every consumer is upgraded"],
        )


def op_create(
    snapshot: Snapshot,
    *,
    item_id: str,
    title: str,
    priority: str = "medium",
    description: str = "",
    needs: list[str] | tuple[str, ...] = (),
    acceptance: list[str] | tuple[str, ...] = (),
    links: list[str] | tuple[str, ...] = (),
    context: str = "",
    batch: dict[str, Any] | None = None,
    not_before: str = "",
    now: datetime | None = None,
) -> str:
    validate_id(item_id)
    hold = normalize_not_before(not_before, now) if not_before else None
    items = snapshot.index["items"]
    if item_id in items:
        raise TodoError(f"duplicate task ID {item_id!r}", code=E_STATE)
    entry = _check_entry_shape(
        item_id,
        {
            "title": title,
            "priority": priority,
            "status": "open",
            "claim": None,
            "needs": list(needs),
        },
    )
    for dep in entry.get("needs", []):
        validate_id(dep)
        if dep not in items:
            raise TodoError(f"task {item_id!r} needs unknown task {dep!r}", code=E_STATE)
    items[item_id] = entry
    previous_version = snapshot.index["schema_version"]
    if batch is not None:
        _require_batch_schema(snapshot, "writing item batch metadata")
    detail: dict[str, Any] = {}
    if description:
        detail["description"] = description
    if acceptance:
        detail["acceptance"] = list(acceptance)
    if links:
        detail["links"] = list(links)
    if context:
        detail["context"] = context
    if hold is not None:
        detail["not_before"] = hold
    if batch is not None:
        detail["batch"] = json.loads(json.dumps(batch))
    snapshot.details[item_id] = detail
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        del items[item_id]
        del snapshot.details[item_id]
        snapshot.index["schema_version"] = previous_version
        raise
    return item_id


def _closeout_started(snapshot: Snapshot, item_id: str) -> bool:
    meta = snapshot.details.get(item_id, {}).get("batch")
    record = snapshot.index.get("batches", {}).get(meta.get("batch_id")) if isinstance(meta, dict) else None
    if not isinstance(record, dict) or record.get("lifecycle", "active") != "active":
        return False
    return any(snapshot.index["items"].get(member, {}).get("status") == "done" for member in record["members"])


def _invalidate_prepared_dependents(snapshot: Snapshot, root_id: str) -> list[str]:
    """Invalidate prepared/final evidence downstream of a changed member."""
    invalidated: list[str] = []
    queue = [root_id]
    seen = {root_id}
    while queue:
        predecessor = queue.pop(0)
        for item_id, detail in snapshot.details.items():
            batch = detail.get("batch")
            deps = batch.get("implementation_dependencies", []) if isinstance(batch, dict) else []
            if predecessor not in deps or item_id in seen:
                continue
            if "prepared" not in detail and "final_evidence" not in detail:
                continue
            invalidated.append(item_id)
            seen.add(item_id)
            queue.append(item_id)
    completed = [item_id for item_id in invalidated if snapshot.index["items"][item_id]["status"] == "done"]
    if completed:
        raise TodoError(
            f"cannot invalidate evidence for completed batch members: {', '.join(sorted(completed))}",
            code=E_STATE,
        )
    for item_id in invalidated:
        snapshot.details[item_id].pop("prepared", None)
        snapshot.details[item_id].pop("final_evidence", None)
    return invalidated


def op_update(
    snapshot: Snapshot,
    item_id: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    needs: list[str] | tuple[str, ...] | None = None,
    acceptance: list[str] | tuple[str, ...] | None = None,
    links: list[str] | tuple[str, ...] | None = None,
    context: str | None = None,
    context_append: str | None = None,
    status: str | None = None,
    batch: dict[str, Any] | None = None,
    not_before: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    items = snapshot.index["items"]
    if item_id not in items:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
    if context_append is not None:
        if context is not None:
            raise TodoError("pass context or context_append, not both", code=E_STATE)
        if not context_append.strip():
            raise TodoError("context_append must not be empty", code=E_STATE)
    # An empty string clears the hold, as it clears the other detail fields.
    hold = normalize_not_before(not_before, now) if not_before else None
    context_only_request = all(
        value is None
        for value in (title, priority, description, needs, acceptance, links, status, batch, not_before)
    )
    batch_settled = False
    existing_batch = snapshot.details[item_id].get("batch")
    if batch is not None and isinstance(existing_batch, dict) and batch != existing_batch:
        raise TodoError(
            f"registered batch metadata for {item_id!r} is immutable; abort the batch before reassignment",
            code=E_STATE,
            recovery=["abort the batch, then register the member again with the new metadata"],
        )
    if isinstance(existing_batch, dict):
        batch_record = snapshot.index.get("batches", {}).get(existing_batch.get("batch_id"))
        if isinstance(batch_record, dict) and batch_record.get("lifecycle", "active") != "active":
            if not context_only_request:
                raise TodoError(
                    f"batch {existing_batch['batch_id']!r} is no longer active", code=E_STATE,
                    recovery=["only context and context_append may be edited after the batch is no longer active"],
                )
            batch_settled = True
    entry = items[item_id]
    previous = json.loads(json.dumps(entry))
    previous_details = json.loads(json.dumps(snapshot.details))
    previous_version = snapshot.index["schema_version"]
    changed: list[str] = []
    if title is not None:
        entry["title"] = title
        changed.append("title")
    if priority is not None:
        entry["priority"] = priority
        changed.append("priority")
    if needs is not None:
        entry["needs"] = list(needs)
        changed.append("needs")
    if status is not None:
        if status not in STATUSES:
            raise TodoError(f"unknown status {status!r}", code=E_STATE)
        if status != entry["status"]:
            # Plain updates may only park and unpark work. Entering active
            # needs a claim (take); leaving active releases or closes it
            # (release/finish); terminal states go through finish/drop.
            # Bypassing those paths would strand or fabricate claims.
            if {entry["status"], status} != UPDATE_STATUSES:
                raise TodoError(
                    f"cannot move {item_id!r} from {entry['status']!r} to {status!r} via update; "
                    "use take/release/finish/drop",
                    code=E_STATE,
                )
            if claim_is_live(entry.get("claim")):
                raise TodoError(
                    f"cannot move claimed task {item_id!r} via update; release it first",
                    code=E_STATE,
                )
            entry["status"] = status
            changed.append("status")
    detail = snapshot.details[item_id]
    if description is not None:
        if description:
            detail["description"] = description
        else:
            detail.pop("description", None)
        changed.append("description")
    if acceptance is not None:
        if list(acceptance):
            detail["acceptance"] = list(acceptance)
        else:
            detail.pop("acceptance", None)
        changed.append("acceptance")
    if links is not None:
        if list(links):
            detail["links"] = list(links)
        else:
            detail.pop("links", None)
        changed.append("links")
    if context is not None:
        if context:
            detail["context"] = context
        else:
            detail.pop("context", None)
        changed.append("context")
    if context_append is not None:
        current = detail.get("context", "")
        detail["context"] = f"{current}\n\n{context_append}" if current else context_append
        changed.append("context")
    if not_before is not None:
        if hold is not None:
            detail["not_before"] = hold
        else:
            detail.pop("not_before", None)
        changed.append("not_before")
    if batch is not None:
        if batch != existing_batch:
            _require_batch_schema(snapshot, "writing item batch metadata")
            detail["batch"] = json.loads(json.dumps(batch))
            changed.append("batch")
    invalidated: list[str] = []
    try:
        context_only = set(changed) <= {"context"}
        closeout = _closeout_started(snapshot, item_id)
        if changed and "prepared" in detail and entry["status"] == "done" and not batch_settled:
            raise TodoError(
                f"cannot edit completed prepared batch member {item_id!r}",
                code=E_STATE,
                recovery=["completed prepared members are immutable; record follow-up work as a new task"],
            )
        if changed and not context_only and "prepared" in detail:
            if closeout:
                raise TodoError(
                    f"closeout has started for the batch of {item_id!r}; receipt-invalidating edits are refused",
                    code=E_STATE,
                    recovery=["finish closeout, then record the change as a new task"],
                )
            # A prepared receipt binds the exact member content and declared
            # edge. Any later edit invalidates it instead of allowing a stale
            # receipt to unlock a dependent member.
            detail.pop("prepared", None)
            detail.pop("final_evidence", None)
            changed.append("prepared_receipt")
        if changed and not context_only:
            invalidated = _invalidate_prepared_dependents(snapshot, item_id)
            if invalidated:
                if closeout:
                    raise TodoError(
                        f"closeout has started for the batch of {item_id!r}; receipt-invalidating edits are refused",
                        code=E_STATE,
                        recovery=["finish closeout, then record the change as a new task"],
                    )
                changed.append("downstream_prepared_receipts")
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        items[item_id] = previous
        snapshot.details.clear()
        snapshot.details.update(previous_details)
        snapshot.index["schema_version"] = previous_version
        raise
    return {"id": item_id, "changed": changed, "invalidated": invalidated}


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _owned_batch(snapshot: Snapshot, batch_id: str, worker: str, owner_generation: str) -> dict[str, Any]:
    """The active v2 batch, after proving owner worker and generation."""
    try:
        batch = snapshot.index["batches"][batch_id]
    except KeyError:
        raise TodoError(f"unknown batch {batch_id!r}", code=E_STATE) from None
    if batch["owner"] != worker or batch["owner_generation"] != owner_generation:
        raise TodoError(
            f"stale batch owner for {batch_id!r}", code=E_CLAIM_STALE,
            recovery=["re-read the owner with show_batch; use your own owner_generation or recover_batch_owner"],
        )
    if batch.get("lifecycle", "active") != "active":
        raise TodoError(
            f"batch {batch_id!r} is no longer active", code=E_STATE,
            recovery=["read the batch with show_batch; a finished or aborted batch accepts no batch operations"],
        )
    if batch_schema_of(batch) != BATCH_SCHEMA:
        raise TodoError(
            f"batch {batch_id!r} predates batch v2",
            code=E_SCHEMA,
            recovery=["finish or abort it with the client that registered it"],
        )
    return batch


def active_batch_of(snapshot: Snapshot, item_id: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    for batch_id, batch in snapshot.index.get("batches", {}).items():
        if batch.get("lifecycle", "active") == "active" and item_id in batch.get("members", []):
            return batch_id, batch
    return None, None


def require_member_owner(snapshot: Snapshot, item_id: str, worker: str) -> None:
    """Reserve claims on active-batch members to the batch owner."""
    batch_id, batch = active_batch_of(snapshot, item_id)
    if batch is not None and batch["owner"] != worker:
        raise TodoError(
            f"task {item_id!r} belongs to active batch {batch_id!r} owned by {batch['owner']!r}; "
            "only the owner may claim it",
            code=E_BATCH_OWNER,
            recovery=[
                f"ask {batch['owner']!r} to take {item_id!r}",
                f"if {batch['owner']!r} is dead, recover_batch_owner for batch {batch_id!r}",
            ],
        )


def _closeout_or_done(snapshot: Snapshot, batch: dict[str, Any]) -> bool:
    return any(snapshot.index["items"].get(member, {}).get("status") == "done" for member in batch["members"])


def op_register_batch(
    snapshot: Snapshot,
    *,
    batch_id: str,
    project_id: str,
    repository: str,
    owner: str,
    owner_generation: str,
    integration_branch: str,
    integration_worktree: str,
    start_head: str,
    members: list[str] | tuple[str, ...],
    scope: dict[str, list[str]],
    scope_hash: str,
    delivery_boundary: str,
    terminal_outcome: str,
    develop_ref: str | None = None,
    confirm_schema3_cutover: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Register one immutable batch contract; the first call may cut the store over."""
    owner = validate_worker(owner)
    previous_version = snapshot.index["schema_version"]
    migrating = previous_version < SCHEMA_VERSION
    if migrating and confirm_schema3_cutover is not True:
        raise TodoError(
            "register_batch requires confirm_schema3_cutover=true after every active consumer is upgraded",
            code=E_STATE,
            recovery=["stop or upgrade every process with access to the state branch, then retry with the flag"],
        )
    if develop_ref is not None:
        _validate_develop_ref(develop_ref)
    batches = snapshot.index.setdefault("batches", {})
    if batch_id in batches:
        raise TodoError(f"batch {batch_id!r} is already registered; refusing duplicate enrollment", code=E_CONFLICT)
    member_set = set(members)
    for existing_id, existing in batches.items():
        if existing.get("lifecycle", "active") == "active" and member_set.intersection(existing.get("members", [])):
            overlap = ", ".join(sorted(member_set.intersection(existing["members"])))
            raise TodoError(
                f"batch members already belong to active batch {existing_id!r}: {overlap}",
                code=E_CONFLICT,
            )
    moment = now or datetime.now(timezone.utc)
    for member in members:
        claim = snapshot.index["items"].get(member, {}).get("claim")
        if claim_is_live(claim, moment) and claim["worker"] != owner:
            raise TodoError(
                f"member {member!r} is claimed by {claim['worker']!r}; the batch owner must be able to claim it",
                code=E_CONFLICT,
                recovery=[f"ask {claim['worker']!r} to release {member!r}, or register after the claim expires"],
            )
    if migrating:
        active = sorted(bid for bid, rec in batches.items() if rec.get("lifecycle", "active") == "active")
        if active:
            raise TodoError(
                f"cutover refused: active batches {active} must be finished or aborted under the writing client first",
                code=E_STATE,
                recovery=["finish or abort the active batches with the client that registered them, then retry"],
            )
        foreign = sorted(
            f"{item_id} ({entry['claim']['worker']})"
            for item_id, entry in snapshot.index["items"].items()
            if claim_is_live(entry.get("claim"), moment) and entry["claim"]["worker"] != owner
        )
        if foreign:
            raise TodoError(
                f"cutover refused: live claims held by other workers: {', '.join(foreign)}",
                code=E_ACTIVE_CLAIMS,
                recovery=["wait for those claims to be released or to expire, then retry"],
            )
    previous_batches = json.loads(json.dumps(batches))
    if migrating:
        snapshot.index["schema_version"] = SCHEMA_VERSION
        for record in batches.values():
            record["batch_schema"] = 1
    record = {
        "batch_schema": BATCH_SCHEMA,
        "project_id": project_id,
        "repository": repository,
        "owner": owner,
        "owner_generation": owner_generation,
        "owner_epoch": 0,
        "integration_branch": integration_branch,
        "integration_worktree": integration_worktree,
        "start_head": start_head,
        "integration_head": start_head,
        "integration_log": [{"seq": 0, "head": start_head, "kind": "start", "at": _now_iso(moment)}],
        "recoveries": [],
        "final_pr_history": [],
        "accepted_members": {},
        "integrated_members": {},
        "members": list(members),
        "scope": json.loads(json.dumps(scope)),
        "scope_hash": scope_hash,
        "delivery_boundary": delivery_boundary,
        "terminal_outcome": terminal_outcome,
        "lifecycle": "active",
    }
    if develop_ref is not None:
        record["develop_ref"] = develop_ref
    batches[batch_id] = record
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        snapshot.index["batches"] = previous_batches
        snapshot.index["schema_version"] = previous_version
        raise
    return {
        "batch_id": batch_id,
        "members": list(members),
        "start_head": start_head,
        "schema_version": snapshot.index["schema_version"],
        "migrated_from": previous_version if migrating else None,
    }


def op_bind_batch_pr(
    snapshot: Snapshot,
    batch_id: str,
    worker: str,
    owner_generation: str,
    *,
    pr_repository: str,
    number: int,
    node_id: str,
    head: str,
    expected_previous_head: str | None = None,
    op_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind, or revise, the single final PR of a batch."""
    worker = validate_worker(worker)
    batch = _owned_batch(snapshot, batch_id, worker, owner_generation)
    final_pr = {"repository": pr_repository, "number": number, "node_id": node_id, "head": head}
    _validate_batch_record(batch_id, {**batch, "final_pr": final_pr})
    if expected_previous_head is not None:
        _validate_revision(expected_previous_head, "expected_previous_head")
    bound = batch.get("final_pr")
    history = batch["final_pr_history"]
    if bound is None:
        if expected_previous_head is not None:
            raise TodoError(
                f"batch {batch_id!r} has no final PR binding to revise",
                code=E_CONFLICT,
                recovery=["bind the final PR without expected_previous_head"],
            )
        if head != batch["integration_head"]:
            raise TodoError(
                f"final PR head is not the registered integration head for {batch_id!r}",
                code=E_CONFLICT,
                recovery=["re-read integration_head with show_batch and bind the PR at that head"],
            )
        batch["final_pr"] = final_pr
        history.append({"previous_head": None, "head": head, "op_id": op_id, "at": _now_iso(now)})
        validate_snapshot(snapshot.index, snapshot.details)
        return {"batch_id": batch_id, "final_pr": final_pr, "idempotent": False}
    if any(bound[key] != final_pr[key] for key in ("repository", "number", "node_id")):
        raise TodoError(
            f"batch {batch_id!r} is bound to a different PR; one batch has one final PR",
            code=E_CONFLICT,
            recovery=["bind the PR already recorded in show_batch final_pr"],
        )
    if head == bound["head"]:
        last_previous = history[-1]["previous_head"] if history else None
        if expected_previous_head is not None and expected_previous_head != last_previous:
            raise TodoError(
                f"stale expected_previous_head for {batch_id!r}",
                code=E_CONFLICT,
                recovery=["re-read final_pr with show_batch and retry"],
            )
        return {"batch_id": batch_id, "final_pr": bound, "previous_head": last_previous, "idempotent": True, "_no_write": True}
    if expected_previous_head is None:
        raise TodoError(
            f"revising the final PR head of {batch_id!r} needs expected_previous_head",
            code=E_CONFLICT,
            recovery=["re-read final_pr with show_batch and pass its head as expected_previous_head"],
        )
    if expected_previous_head != bound["head"]:
        raise TodoError(
            f"stale expected_previous_head for {batch_id!r}",
            code=E_CONFLICT,
            recovery=["re-read final_pr with show_batch and retry"],
        )
    if head != batch["integration_head"]:
        raise TodoError(
            f"final PR head is not the registered integration head for {batch_id!r}",
            code=E_CONFLICT,
            recovery=["re-read integration_head with show_batch and bind the PR at that head"],
        )
    if _closeout_or_done(snapshot, batch):
        raise TodoError(
            f"closeout has started for {batch_id!r}; the final PR head can no longer change", code=E_STATE,
            recovery=["resume closeout with the registered final PR head"],
        )
    batch["final_pr"] = final_pr
    history.append({"previous_head": bound["head"], "head": head, "op_id": op_id, "at": _now_iso(now)})
    for member in batch["members"]:
        snapshot.details.get(member, {}).pop("final_evidence", None)
    validate_snapshot(snapshot.index, snapshot.details)
    return {"batch_id": batch_id, "final_pr": final_pr, "previous_head": bound["head"], "idempotent": False}


def op_advance_batch_head(
    snapshot: Snapshot,
    batch_id: str,
    worker: str,
    owner_generation: str,
    *,
    expected_head: str,
    head: str,
    reason: str,
    changed_files: list[str] | tuple[str, ...],
    merged_parent: str | None = None,
    op_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a leader fix or develop merge as the new integration head."""
    worker = validate_worker(worker)
    batch = _owned_batch(snapshot, batch_id, worker, owner_generation)
    if reason not in ADVANCE_REASONS:
        raise TodoError(
            f"reason must be one of {ADVANCE_REASONS}", code=E_STATE,
            recovery=[f"retry with reason set to one of {list(ADVANCE_REASONS)}"],
        )
    _validate_revision(expected_head, "expected_head")
    _validate_revision(head, "head")
    files = _validate_path_list("changed_files", sorted(set(changed_files)))
    log = batch["integration_log"]
    for entry in log:
        if (
            entry["kind"] == reason
            and entry["head"] == head
            and entry["previous_head"] == expected_head
            and entry.get("changed_files") == files
        ):
            return {
                "batch_id": batch_id, "integration_head": head, "seq": entry["seq"], "reason": reason,
                "changed_files": files, "requires_acceptance": list(entry.get("requires_acceptance", [])),
                "idempotent": True, "_no_write": True,
            }
    if _closeout_or_done(snapshot, batch):
        raise TodoError(
            f"closeout has started for {batch_id!r}; resume closeout instead of changing the head", code=E_STATE,
            recovery=["resume closeout with the registered integration head"],
        )
    if expected_head != batch["integration_head"]:
        raise TodoError(
            f"stale expected head for {batch_id!r}; re-read with show_batch and retry",
            code=E_CONFLICT,
            recovery=["re-read integration_head with show_batch and retry with it as expected_head"],
        )
    if head == expected_head or any(entry["head"] == head for entry in log):
        raise TodoError(
            f"head {head} is already recorded; an advance needs a new head",
            code=E_BATCH_HEAD,
            recovery=["create a new commit and advance to its head"],
        )
    entry: dict[str, Any] = {
        "seq": len(log), "head": head, "previous_head": expected_head, "kind": reason,
        "changed_files": files, "op_id": op_id, "at": _now_iso(now),
    }
    if reason == "fix":
        union = [pattern for member in batch["members"] for pattern in batch["scope"][member]]
        outside = [path for path in files if not any(fnmatch.fnmatch(path, pattern) for pattern in union)]
        if outside:
            raise TodoError(
                f"a fix cannot widen scope; outside the member scopes: {outside[:5]}",
                code=E_BATCH_HEAD,
                recovery=["split member changes into a fix advance limited to the registered scope"],
            )
        touched = []
        for member, receipt in _v2_receipts(snapshot, batch_id, batch).items():
            if set(files) & set(receipt.get("changed_files", [])):
                touched.append(member)
        entry["requires_acceptance"] = sorted(touched)
    else:
        if "develop_ref" not in batch:
            raise TodoError(
                f"batch {batch_id!r} was registered without a develop_ref; develop merges cannot be recorded",
                code=E_BATCH_HEAD,
                recovery=["record the change as a fix advance or register the batch with a develop_ref"],
            )
        if merged_parent is None:
            raise TodoError(
                "a develop merge needs its merged parent",
                code=E_BATCH_HEAD,
                recovery=["pass merged_parent with the develop commit that was merged"],
            )
        entry["merged_parent"] = _validate_revision(merged_parent, "merged_parent")
    log.append(entry)
    batch["integration_head"] = head
    validate_snapshot(snapshot.index, snapshot.details)
    return {
        "batch_id": batch_id, "integration_head": head, "seq": entry["seq"], "reason": reason,
        "changed_files": files, "requires_acceptance": list(entry.get("requires_acceptance", [])),
        "idempotent": False,
    }


def op_recover_batch_owner(
    snapshot: Snapshot,
    batch_id: str,
    worker: str,
    *,
    expected_owner: str,
    expected_owner_generation: str,
    new_owner_generation: str,
    reason: str,
    expected_integration_worktree: str | None = None,
    expected_integration_head: str | None = None,
    integration_worktree: str | None = None,
    session_id: str | None = None,
    client: str | None = None,
    op_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Take batch ownership from a dead leader; the caller names the new generation."""
    worker = validate_worker(worker)
    expected_owner = validate_worker(expected_owner)
    reason = validate_takeover_reason(reason)
    if not isinstance(new_owner_generation, str) or not GENERATION_RE.fullmatch(new_owner_generation):
        raise TodoError(
            "new_owner_generation must be 32 lowercase hex characters", code=E_STATE,
            recovery=["generate a fresh 32-character lowercase hex owner generation and retry"],
        )
    relocation_requested = (
        expected_integration_worktree is not None
        or expected_integration_head is not None
        or integration_worktree is not None
    )
    if relocation_requested:
        if (
            not isinstance(expected_integration_worktree, str)
            or not isinstance(expected_integration_head, str)
            or not isinstance(integration_worktree, str)
            or not os.path.isabs(expected_integration_worktree)
            or not os.path.isabs(integration_worktree)
        ):
            raise TodoError(
                "relocation requires all of expected_integration_worktree, expected_integration_head, "
                "and integration_worktree as absolute paths / full revisions",
                code=E_STATE,
                recovery=["pass all three relocation parameters as absolute paths and a full revision"],
            )
        _validate_revision(expected_integration_head, "expected_integration_head")
    try:
        batch = snapshot.index["batches"][batch_id]
    except KeyError:
        raise TodoError(f"unknown batch {batch_id!r}", code=E_STATE) from None
    if batch.get("lifecycle", "active") != "active":
        raise TodoError(
            f"batch {batch_id!r} is no longer active", code=E_STATE,
            recovery=["read the batch with show_batch; a finished or aborted batch accepts no batch operations"],
        )
    if batch_schema_of(batch) != BATCH_SCHEMA:
        raise TodoError(
            f"batch {batch_id!r} predates batch v2",
            code=E_SCHEMA,
            recovery=["finish or abort it with the client that registered it"],
        )
    recoveries = batch["recoveries"]
    members = list(batch["members"])
    if (
        batch["owner"] == worker
        and batch["owner_generation"] == new_owner_generation
        and recoveries
        and recoveries[-1]["previous_owner"] == expected_owner
    ):
        last_recovery = recoveries[-1]
        last_reloc = last_recovery.get("relocation")
        if relocation_requested:
            expected_reloc = {
                "expected_integration_worktree": expected_integration_worktree,
                "expected_integration_head": expected_integration_head,
                "integration_worktree": integration_worktree,
            }
            if not isinstance(last_reloc, dict) or {
                k: last_reloc.get(k) for k in expected_reloc
            } != expected_reloc:
                raise TodoError(
                    f"retry with the same new owner generation for {batch_id!r} with different relocation is refused",
                    code=E_CONFLICT,
                    recovery=["generate a fresh 32-character lowercase hex owner generation and retry"],
                )
        elif last_reloc is not None:
            raise TodoError(
                f"retry with the same new owner generation for {batch_id!r} without relocation is refused",
                code=E_CONFLICT,
                recovery=["generate a fresh 32-character lowercase hex owner generation and retry"],
            )
        if last_recovery.get("reason") != reason:
            raise TodoError(
                f"retry with the same new owner generation for {batch_id!r} with different reason is refused",
                code=E_CONFLICT,
                recovery=["generate a fresh 32-character lowercase hex owner generation and retry"],
            )
        return {
            "batch_id": batch_id, "owner": worker, "owner_epoch": batch["owner_epoch"],
            "recovered_members": members, "idempotent": True, "_no_write": True,
        }
    if batch["owner"] != expected_owner:
        raise TodoError(
            f"owner of {batch_id!r} changed; re-read with show_batch and retry",
            code=E_CONFLICT,
            recovery=["re-read the owner with show_batch"],
        )
    if batch["owner_generation"] != expected_owner_generation:
        raise TodoError(
            f"stale owner generation for {batch_id!r}",
            code=E_CLAIM_STALE,
            recovery=["re-read the owner with show_batch"],
        )
    if relocation_requested:
        if batch.get("integration_worktree") != expected_integration_worktree:
            raise TodoError(
                f"expected integration worktree for {batch_id!r} changed; re-read with show_batch and retry",
                code=E_CONFLICT,
                recovery=["re-read the batch with show_batch"],
            )
        if batch.get("integration_head") != expected_integration_head:
            raise TodoError(
                f"expected integration head for {batch_id!r} changed; re-read with show_batch and retry",
                code=E_CONFLICT,
                recovery=["re-read the batch with show_batch"],
            )
    if new_owner_generation == batch["owner_generation"]:
        raise TodoError(
            "new_owner_generation must differ from the current generation", code=E_STATE,
            recovery=["generate a fresh 32-character lowercase hex owner generation and retry"],
        )
    previous_owner = batch["owner"]
    previous_worktree = batch["integration_worktree"]
    if relocation_requested:
        batch["integration_worktree"] = integration_worktree
    batch["owner"] = worker
    batch["owner_generation"] = new_owner_generation
    batch["owner_epoch"] += 1
    recovery_entry: dict[str, Any] = {
        "epoch": batch["owner_epoch"], "previous_owner": previous_owner, "new_owner": worker,
        "reason": reason, "session_id": session_id, "client": client, "op_id": op_id, "at": _now_iso(now),
    }
    if relocation_requested:
        recovery_entry["relocation"] = {
            "expected_integration_worktree": expected_integration_worktree,
            "expected_integration_head": expected_integration_head,
            "previous_integration_worktree": previous_worktree,
            "integration_worktree": integration_worktree,
        }
    recoveries.append(recovery_entry)
    validate_snapshot(snapshot.index, snapshot.details)
    out: dict[str, Any] = {
        "batch_id": batch_id, "owner": worker, "owner_epoch": batch["owner_epoch"],
        "recovered_members": members, "idempotent": False, "previous_owner": previous_owner,
    }
    if relocation_requested:
        out["relocation"] = {
            "previous_integration_worktree": previous_worktree,
            "integration_worktree": integration_worktree,
        }
    return out


def op_abort_batch(snapshot: Snapshot, batch_id: str, worker: str, owner_generation: str) -> dict[str, Any]:
    """Abort an owned batch without manufacturing terminal member completion."""
    worker = validate_worker(worker)
    try:
        batch = snapshot.index["batches"][batch_id]
    except KeyError:
        raise TodoError(f"unknown batch {batch_id!r}", code=E_STATE) from None
    if batch["owner"] != worker or batch["owner_generation"] != owner_generation:
        raise TodoError(f"stale batch owner for {batch_id!r}", code=E_CLAIM_STALE)
    lifecycle = batch.get("lifecycle", "active")
    if lifecycle == "completed":
        raise TodoError(f"completed batch {batch_id!r} cannot be aborted", code=E_STATE)
    if lifecycle == "aborted":
        return {"batch_id": batch_id, "lifecycle": "aborted", "idempotent": True}
    if any(
        claim_is_live(snapshot.index["items"].get(member, {}).get("claim"))
        for member in batch["members"]
    ):
        raise TodoError(f"batch {batch_id!r} has a live member claim; release it before abort", code=E_ACTIVE_CLAIMS)
    if any(snapshot.index["items"].get(member, {}).get("status") == "done" for member in batch["members"]):
        raise TodoError(f"batch {batch_id!r} has completed members; resume closeout instead of aborting", code=E_STATE)
    affected: list[str] = []
    for member in batch["members"]:
        detail = snapshot.details.get(member, {})
        lost_receipt = any(key in detail for key in ("prepared", "final_evidence", "batch"))
        detail.pop("prepared", None)
        detail.pop("final_evidence", None)
        # Keep the aborted registry as an audit record, but detach every
        # recoverable member so it returns to the ordinary claim/finish
        # lifecycle. Otherwise the archived lifecycle would strand the item
        # behind the batch-specific guards in prepare, update, and finish.
        detail.pop("batch", None)
        entry = snapshot.index["items"].get(member)
        if entry is not None:
            had_claim = entry.get("claim") is not None
            status_reset = entry.get("status") in ("blocked", "active")
            entry["claim"] = None
            if entry["status"] in ("open", "blocked", "active"):
                entry["status"] = "open"
            # Registry members without an index entry (phantom members)
            # are skipped: only tasks that actually changed qualify, and
            # recording on an unknown task would fail the whole abort.
            if lost_receipt or had_claim or status_reset:
                affected.append(member)
    batch["accepted_members"] = {}
    batch["integrated_members"] = {}
    batch["lifecycle"] = "aborted"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"batch_id": batch_id, "lifecycle": "aborted", "idempotent": False, "affected": affected}


def _require_entry(snapshot: Snapshot, item_id: str) -> dict[str, Any]:
    try:
        return snapshot.index["items"][item_id]
    except KeyError:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE) from None


def op_take(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    require_member_owner(snapshot, item_id, worker)
    moment = now or datetime.now(timezone.utc)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    claim = entry.get("claim")
    if claim_is_live(claim, moment):
        if claim["worker"] == worker:
            # Restart re-adoption rotates the generation: the returning
            # worker resumes with fresh credentials, and any earlier
            # process image holding the old generation goes stale instead
            # of sharing control of the claim.
            claim["generation"] = new_generation()
            claim["expires_at"] = (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            claim["renewals"] = int(claim.get("renewals", 0)) + 1
            entry["status"] = "active"
            validate_snapshot(snapshot.index, snapshot.details)
            return {"id": item_id, "claim": dict(claim), "adopted": True}
        raise TodoError(f"task {item_id!r} is claimed by {claim['worker']!r}", code=E_CONFLICT)
    if entry["status"] not in ("open", "blocked", "active"):
        raise TodoError(f"task {item_id!r} is {entry['status']} and cannot be taken", code=E_STATE)
    fresh = {
        "worker": worker,
        "expires_at": (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generation": new_generation(),
        "renewals": 0,
    }
    entry["claim"] = fresh
    entry["status"] = "active"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": fresh, "adopted": False}


def op_takeover(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    expected_holder: str,
    reason: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Take over a live claim held by a session that cannot resume.

    Unlike `take` (same-worker re-adoption or fresh acquisition), this
    overwrites a *foreign* live claim — but only the exact claim the
    caller inspected: the holder must still match *expected_holder*.
    Anything else (no live claim, same worker, changed holder, terminal
    state) is refused with direction to the ordinary path. Members of an
    active batch are reserved to the batch owner (``E_BATCH_OWNER``); a dead
    owner is replaced with ``op_recover_batch_owner``, never as a side
    effect of taking over a member.
    """
    worker = validate_worker(worker)
    expected_holder = validate_worker(expected_holder)
    reason = validate_takeover_reason(reason)
    entry = _require_entry(snapshot, item_id)
    require_member_owner(snapshot, item_id, worker)
    moment = now or datetime.now(timezone.utc)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    claim = entry.get("claim")
    if not claim_is_live(claim, moment):
        raise TodoError(f"task {item_id!r} has no live claim; use take", code=E_STATE)
    if entry.get("status") != "active":
        raise TodoError(f"task {item_id!r} is {entry.get('status')} and cannot be taken over", code=E_STATE)
    if claim["worker"] == worker:
        raise TodoError(f"task {item_id!r} is already claimed by you; use take to re-adopt", code=E_STATE)
    if claim["worker"] != expected_holder:
        raise TodoError(f"task {item_id!r} holder changed; re-read with show and retry", code=E_CONFLICT)
    displaced = claim["worker"]
    fresh = {
        "worker": worker,
        "expires_at": (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generation": new_generation(),
        "renewals": 0,
    }
    entry["claim"] = fresh
    entry["status"] = "active"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": fresh, "displaced": displaced}


def op_renew(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    moment = now or datetime.now(timezone.utc)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: re-read with show and retry", code=E_CLAIM_STALE)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    claim["expires_at"] = (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The counter guarantees a renewal is always a state change, even when
    # the second-resolution expiry string is unchanged, and it makes the
    # bounded renewal path observable.
    claim["renewals"] = int(claim.get("renewals", 0)) + 1
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": dict(claim)}


def op_release(
    snapshot: Snapshot, item_id: str, worker: str, generation: str
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: cannot release another holder's task", code=E_CLAIM_STALE)
    entry["claim"] = None
    if entry["status"] == "active":
        entry["status"] = "open"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": entry["status"]}


REVIEW_OUTCOMES = ("clear", "not-reviewed", "override")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_prepare_verification(item_id: str, verification: Any, revision: str) -> dict[str, Any]:
    if not isinstance(verification, dict):
        raise TodoError(f"prepare for {item_id!r} requires a verification object", code=E_STATE)
    if verification.get("status") != "passed":
        raise TodoError(f"prepare for {item_id!r} requires passed verification", code=E_STATE)
    if verification.get("revision") != revision:
        raise TodoError(f"prepare for {item_id!r} verification does not match source_revision", code=E_STATE)
    if verification.get("clean") is not True:
        raise TodoError(f"prepare for {item_id!r} requires a clean source checkout", code=E_STATE)
    suite = verification.get("suite")
    if not isinstance(suite, str) or not suite.strip():
        raise TodoError(f"prepare for {item_id!r} requires a bounded verification suite", code=E_STATE)
    command = verification.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part.strip() or _CONTROL_CHARS_RE.search(part) for part in command)
        or any(character.isspace() for character in command[0])
    ):
        raise TodoError(
            f"prepare for {item_id!r} requires a verification command that is a non-empty argv list of "
            "single-line strings starting with an executable name",
            code=E_STATE,
            recovery=["pass the complete argv list the verification ran, one string per argument"],
        )
    review = verification.get("review")
    if not isinstance(review, dict) or review.get("outcome") not in REVIEW_OUTCOMES:
        raise TodoError(
            f"prepare for {item_id!r} requires verification.review.outcome to be one of {', '.join(REVIEW_OUTCOMES)}",
            code=E_STATE,
            recovery=["run the review for the exact accepted head and pass its outcome in verification.review"],
        )
    return verification


def op_prepare(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    *,
    batch_id: str,
    member_id: str,
    source_worktree: str,
    source_revision: str,
    source_base: str,
    accepted_head: str,
    integration_head: str,
    scope_hash: str,
    verification: dict[str, Any],
    changed_files: list[str] | tuple[str, ...] = (),
    implementation_dependencies: list[str] | tuple[str, ...] = (),
    op_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist prepared evidence and release the member claim.

    Preparation is intentionally not completion.  The item returns to ``open``
    with no claim, and only an explicitly registered same-batch implementation
    edge may use its receipt for readiness.  The service validates the actual
    Git checkout before calling this pure operation; this function validates
    the durable shape, claim authority, recorded-head lineage and overlap
    accounting again on every publication retry.
    """
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if not claim or claim.get("worker") != worker or claim.get("generation") != generation:
        raise TodoError(f"stale claim for {item_id!r}: only the holder can prepare", code=E_CLAIM_STALE)
    require_member_owner(snapshot, item_id, worker)
    if entry["status"] != "active":
        raise TodoError(f"task {item_id!r} must be active before it can be prepared", code=E_STATE)
    validate_id(batch_id)
    validate_id(member_id)
    validate_id(item_id)
    source_worktree = str(source_worktree)
    source_revision = _validate_revision(source_revision, "source_revision")
    source_base = _validate_revision(source_base, "source_base")
    accepted_head = _validate_revision(accepted_head, "accepted_head")
    integration_head = _validate_revision(integration_head, "integration_head")
    if accepted_head != source_revision:
        raise TodoError("prepared accepted head must identify the exact source revision", code=E_STATE)
    if not isinstance(scope_hash, str) or not SCOPE_HASH_RE.fullmatch(scope_hash):
        raise TodoError("scope_hash must be a full SHA-256 digest", code=E_STATE)
    verification = _validate_prepare_verification(item_id, verification, source_revision)
    changed_files = sorted(set(changed_files))
    deps = list(implementation_dependencies)
    if len(set(deps)) != len(deps) or any(dep not in entry.get("needs", []) for dep in deps):
        raise TodoError(
            f"prepared implementation dependencies for {item_id!r} must be unique members of needs",
            code=E_STATE,
        )
    detail = snapshot.details[item_id]
    batch = detail.get("batch")
    batch_record = snapshot.index.get("batches", {}).get(batch_id)
    if (
        not isinstance(batch, dict)
        or not isinstance(batch_record, dict)
        or (batch.get("batch_id"), batch.get("member_id")) != (batch_id, member_id)
    ):
        raise TodoError(
            f"task {item_id!r} must be explicitly registered as batch member {member_id!r} before prepare",
            code=E_STATE,
        )
    if batch_record.get("lifecycle", "active") != "active":
        raise TodoError(
            f"batch {batch_id!r} is no longer active", code=E_STATE,
            recovery=["read the batch with show_batch; a finished or aborted batch accepts no batch operations"],
        )
    if batch_schema_of(batch_record) != BATCH_SCHEMA:
        raise TodoError(
            f"batch {batch_id!r} predates batch v2",
            code=E_SCHEMA,
            recovery=["finish or abort it with the client that registered it"],
        )
    registered_deps = list(batch.get("implementation_dependencies", []))
    if registered_deps != deps:
        raise TodoError("prepare dependencies differ from registered batch metadata", code=E_STATE)
    log = batch_record["integration_log"]
    base_seqs = [row["seq"] for row in log if row["head"] == source_base]
    if not base_seqs:
        raise TodoError(
            "source base is neither the batch start head nor a recorded integration head",
            code=E_BATCH_HEAD,
            recovery=["merge the current integration head into the member branch and prepare from it"],
        )
    if scope_hash != batch_record["scope_hash"]:
        raise TodoError("prepared source does not match the registered batch scope", code=E_STATE)
    if member_id not in batch_record["members"]:
        raise TodoError("prepared member is not declared in the batch", code=E_STATE)
    if detail.get("prepared") is not None:
        raise TodoError(f"task {item_id!r} already has prepared evidence", code=E_STATE)
    registered_head = batch_record["integration_head"]
    reprepare = member_id in batch_record.get("integrated_members", {}) and integration_head == registered_head
    if not reprepare and any(row["head"] == integration_head for row in log):
        raise TodoError(
            "prepared integration head must be new; record leader commits with advance_batch_head first",
            code=E_BATCH_HEAD,
            recovery=["record leader commits with advance_batch_head, then prepare from the new head"],
        )
    previous_detail = json.loads(json.dumps(detail))
    previous_batch_record = json.loads(json.dumps(batch_record))
    base_seq = min(base_seqs)
    overlaps: list[dict[str, Any]] = []
    for other, integrated in batch_record.get("integrated_members", {}).items():
        if other == member_id:
            continue
        shared = sorted(set(changed_files) & set(integrated.get("changed_files", [])))
        if shared:
            overlaps.append({"kind": "member", "member": other, "paths": shared})
    for row in log:
        if row["kind"] == "fix" and row["seq"] > base_seq:
            shared = sorted(set(changed_files) & set(row["changed_files"]))
            if shared:
                overlaps.append({"kind": "fix", "seq": row["seq"], "paths": shared})
    seq = len(log)
    log.append({
        "seq": seq, "head": integration_head, "previous_head": registered_head,
        "kind": "reprepare" if reprepare else "prepare", "member": member_id,
        "op_id": op_id, "at": _now_iso(now),
    })
    detail["prepared"] = {
        "schema": PREPARED_RECEIPT_SCHEMA,
        "batch_id": batch_id,
        "member_id": member_id,
        "owner_epoch": batch_record["owner_epoch"],
        "source_worktree": source_worktree,
        "source_revision": source_revision,
        "source_base": source_base,
        "accepted_head": accepted_head,
        "integration_head": integration_head,
        "integration_seq": seq,
        "scope_hash": scope_hash,
        "changed_files": changed_files,
        "overlaps": overlaps,
        "verification": json.loads(json.dumps(verification)),
    }
    batch_record["integration_head"] = integration_head
    batch_record.setdefault("accepted_members", {})[member_id] = accepted_head
    batch_record.setdefault("integrated_members", {})[member_id] = {
        "accepted_head": accepted_head,
        "integration_head": integration_head,
        "integration_seq": seq,
        "changed_files": changed_files,
    }
    entry["claim"] = None
    entry["status"] = "open"
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        snapshot.details[item_id] = previous_detail
        snapshot.index["batches"][batch_id] = previous_batch_record
        raise
    return {
        "id": item_id,
        "status": "open",
        "prepared": {
            "batch_id": batch_id,
            "member_id": member_id,
            "source_worktree": source_worktree,
            "source_revision": source_revision,
            "integration_head": integration_head,
            "integration_seq": seq,
            "overlaps": overlaps,
        },
    }


def op_finish(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    final_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close a task, requiring final-tree evidence for prepared work."""
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: only the holder can finish", code=E_CLAIM_STALE)
    require_member_owner(snapshot, item_id, worker)
    detail = snapshot.details[item_id]
    blocking = [row["id"] for row in open_deferrals(snapshot, item_id)]
    if blocking:
        raise TodoError(
            f"task {item_id!r} has open deferrals {sorted(blocking)}; "
            "promote or dismiss them before finish",
            code=E_OPEN_DEFERRALS,
        )
    batch = snapshot.index.get("batches", {}).get((detail.get("batch") or {}).get("batch_id"))
    if isinstance(batch, dict) and batch.get("lifecycle", "active") != "active":
        raise TodoError(f"batch {(detail.get('batch') or {}).get('batch_id')!r} is no longer active", code=E_STATE)
    previous_detail = json.loads(json.dumps(detail))
    previous_entry = json.loads(json.dumps(entry))
    previous_batch = json.loads(json.dumps(batch)) if isinstance(batch, dict) else None
    if isinstance(batch, dict) and detail.get("prepared") is None:
        raise TodoError(
            f"registered batch member {item_id!r} must be prepared before finish",
            code=E_FINAL_EVIDENCE,
        )
    if detail.get("prepared") is not None:
        if final_evidence is None:
            raise TodoError(
                f"prepared task {item_id!r} needs final-tree evidence before finish",
                code=E_FINAL_EVIDENCE,
            )
        detail["final_evidence"] = _validate_batch_final_evidence(
            snapshot, item_id, _validate_final_evidence(item_id, final_evidence)
        )
    entry["claim"] = None
    entry["status"] = "done"
    if isinstance(batch, dict) and all(
        snapshot.index["items"].get(member, {}).get("status") == "done" for member in batch["members"]
    ):
        batch["lifecycle"] = "completed"
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        snapshot.details[item_id] = previous_detail
        snapshot.index["items"][item_id] = previous_entry
        if previous_batch is not None:
            snapshot.index["batches"][batch["batch_id"]] = previous_batch
        raise
    return {"id": item_id, "status": "done"}


def _next_deferral_id(detail: dict[str, Any]) -> int:
    rows = detail.get("deferrals")
    if not rows:
        return 1
    return max(row["id"] for row in rows if isinstance(row, dict)) + 1


def _find_deferral(detail: dict[str, Any], deferral_id: int) -> dict[str, Any]:
    for row in _deferrals_of(detail):
        if isinstance(row, dict) and row.get("id") == deferral_id:
            return row
    raise TodoError(f"unknown deferral {deferral_id}", code=E_STATE)


def query_deferrals(
    snapshot: Snapshot,
    *,
    from_item: str | None = None,
    resolution: str | None = None,
) -> list[dict[str, Any]]:
    """Return deferral rows across tasks, ordered by source then id.

    ``resolution`` defaults to ``open`` when omitted so the queue view
    stays actionable; pass ``"all"`` for every row.
    """
    wanted = "open" if resolution is None else resolution
    if wanted != "all" and wanted not in DEFERRAL_RESOLUTIONS:
        raise TodoError(f"unknown deferral resolution {resolution!r}", code=E_STATE)
    if from_item is not None:
        if from_item not in snapshot.index["items"]:
            raise TodoError(f"unknown task {from_item!r}", code=E_STATE)
        sources = [from_item]
    else:
        sources = sorted(snapshot.index["items"])
    rows: list[dict[str, Any]] = []
    for item_id in sources:
        for row in _deferrals_of(snapshot.details.get(item_id, {})):
            if wanted != "all" and row.get("resolution", "open") != wanted:
                continue
            rows.append(json.loads(json.dumps(row)))
    return rows


def op_defer(
    snapshot: Snapshot,
    item_id: str,
    *,
    summary: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record deferred work on a task. The task keeps its status and claim."""
    entry = _require_entry(snapshot, item_id)
    if entry["status"] in TERMINAL_STATUSES:
        raise TodoError(f"cannot defer on {entry['status']} task {item_id!r}", code=E_STATE)
    summary = validate_deferral_summary(summary)
    reason = validate_deferral_reason(reason)
    detail = snapshot.details[item_id]
    rows = detail.setdefault("deferrals", [])
    if not isinstance(rows, list):
        raise TodoError(f"detail for {item_id!r}: 'deferrals' must be a list", code=E_STATE)
    number = _next_deferral_id(detail)
    moment = now or datetime.now(timezone.utc)
    row = {
        "id": number,
        "from_item": item_id,
        "summary": summary,
        "reason": reason,
        "resolution": "open",
        "created_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    rows.append(row)
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        rows.pop()
        if not rows:
            detail.pop("deferrals", None)
        raise
    return json.loads(json.dumps(row))


def op_promote_deferral(
    snapshot: Snapshot,
    item_id: str,
    deferral_id: int,
    *,
    to_item: str | None = None,
    title: str | None = None,
    priority: str = "medium",
    description: str = "",
    needs: list[str] | tuple[str, ...] = (),
    acceptance: list[str] | tuple[str, ...] = (),
    links: list[str] | tuple[str, ...] = (),
    context: str = "",
    batch: dict[str, Any] | None = None,
    not_before: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve an open deferral by creating (or linking) its successor item.

    The successor is created first with the ordinary create validation,
    then the deferral row flips to ``promoted`` with ``resolved_item`` —
    one atomic snapshot change, so a failure leaves neither a stray item
    nor a half-resolved row.
    """
    _require_entry(snapshot, item_id)
    if not isinstance(deferral_id, int) or isinstance(deferral_id, bool):
        raise TodoError(f"invalid deferral id {deferral_id!r}", code=E_STATE)
    detail = snapshot.details[item_id]
    row = _find_deferral(detail, deferral_id)
    if row.get("resolution", "open") != "open":
        raise TodoError(
            f"deferral {deferral_id} for {item_id!r} is already {row.get('resolution')}",
            code=E_STATE,
        )
    moment = now or datetime.now(timezone.utc)
    if to_item is not None:
        validate_id(to_item)
        if to_item not in snapshot.index["items"]:
            raise TodoError(f"promotion target {to_item!r} is unknown", code=E_STATE)
        if to_item == item_id:
            raise TodoError("promotion target must differ from its source task", code=E_STATE)
        successor = to_item
        created = False
    else:
        successor = f"{item_id}-deferral-{deferral_id}"
        if successor in snapshot.index["items"]:
            raise TodoError(
                f"promotion target {successor!r} already exists; pass to_item explicitly",
                code=E_CONFLICT,
            )
        op_create(
            snapshot,
            item_id=successor,
            title=title or row["summary"],
            priority=priority,
            description=description or f"Promoted from deferral {deferral_id} on {item_id}: {row['reason']}",
            needs=list(needs),
            acceptance=list(acceptance),
            links=list(links),
            context=context,
            batch=batch,
            not_before=not_before,
            now=moment,
        )
        created = True
    previous = json.loads(json.dumps(row))
    row["resolution"] = "promoted"
    row["resolved_item"] = successor
    row.pop("resolved_reason", None)
    row["resolved_at"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        if created:
            del snapshot.index["items"][successor]
            del snapshot.details[successor]
        row.clear()
        row.update(previous)
        raise
    return {"id": deferral_id, "from_item": item_id, "resolution": "promoted", "resolved_item": successor}


def op_dismiss_deferral(
    snapshot: Snapshot,
    item_id: str,
    deferral_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve an open deferral as deliberately dropped, with a reason."""
    _require_entry(snapshot, item_id)
    if not isinstance(deferral_id, int) or isinstance(deferral_id, bool):
        raise TodoError(f"invalid deferral id {deferral_id!r}", code=E_STATE)
    reason = validate_deferral_reason(reason)
    detail = snapshot.details[item_id]
    row = _find_deferral(detail, deferral_id)
    if row.get("resolution", "open") != "open":
        raise TodoError(
            f"deferral {deferral_id} for {item_id!r} is already {row.get('resolution')}",
            code=E_STATE,
        )
    moment = now or datetime.now(timezone.utc)
    previous = json.loads(json.dumps(row))
    row["resolution"] = "dismissed"
    row.pop("resolved_item", None)
    row["resolved_reason"] = reason
    row["resolved_at"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        row.clear()
        row.update(previous)
        raise
    return {"id": deferral_id, "from_item": item_id, "resolution": "dismissed"}


def op_drop(
    snapshot: Snapshot,
    item_id: str,
    worker: str | None = None,
    generation: str | None = None,
    *,
    reason: str,
) -> dict[str, Any]:
    """Abandon a task and record why. A live claim needs its holder's
    generation, exactly like release/finish: a stale process image must not
    terminally drop work another process re-adopted. Unclaimed tasks drop
    without credentials. The reason lands in the detail as ``drop_reason``
    in the same write as the status change; it is validated before any
    mutation."""
    reason = validate_drop_reason(reason)
    entry = _require_entry(snapshot, item_id)
    if entry["status"] in TERMINAL_STATUSES:
        raise TodoError(f"task {item_id!r} is already {entry['status']}", code=E_STATE)
    claim = entry.get("claim")
    detail = snapshot.details[item_id]
    batch_meta = detail.get("batch")
    batch = snapshot.index.get("batches", {}).get(batch_meta.get("batch_id")) if isinstance(batch_meta, dict) else None
    if isinstance(batch, dict) and batch.get("lifecycle", "active") == "active" and "prepared" in detail:
        raise TodoError(
            f"prepared batch member {item_id!r} cannot be dropped; abort or complete its batch",
            code=E_STATE,
        )
    if claim_is_live(claim):
        if (
            worker is None
            or generation is None
            or claim.get("worker") != validate_worker(worker)
            or claim.get("generation") != generation
        ):
            holder = claim["worker"] if claim else "?"
            raise TodoError(
                f"stale claim for {item_id!r}: held by {holder!r}; only the holder can drop",
                code=E_CLAIM_STALE,
            )
    entry["claim"] = None
    entry["status"] = "dropped"
    detail["drop_reason"] = reason
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": "dropped"}


def assert_quiescent(snapshot: Snapshot, now: datetime | None = None) -> None:
    """Refuse cutover/migration while live claims exist."""
    moment = now or datetime.now(timezone.utc)
    holders = sorted(
        {entry["claim"]["worker"] for entry in snapshot.index["items"].values() if claim_is_live(entry.get("claim"), moment)}
    )
    if holders:
        raise TodoError(
            f"refusing: live claims held by {', '.join(holders)}; "
            "wait for release/finish or expiry before cutover",
            code=E_ACTIVE_CLAIMS,
        )


def ready_rows(snapshot: Snapshot, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    return query_items(snapshot, ready_only=True, now=now)
