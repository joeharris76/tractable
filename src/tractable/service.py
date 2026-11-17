"""Shared application operations over the JSON/Git state branch.

This module backs both the MCP agent interface and the human/CI CLI, so
transport logic is never duplicated. Every response is capped at its final
model-visible serialization (:data:`MAX_BYTES`): size checks use the same
indented JSON the MCP transport emits, not a compact encoding. Lists page
with stable cursors scoped to a state revision plus the query that produced
them; oversized fields report a usable section read instead of looping or
truncating silently.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import git_backend
from . import store as S
from .errors import (
    E_BATCH_HEAD,
    E_CONFLICT,
    E_CURSOR_STALE,
    E_FINAL_EVIDENCE,
    E_MULTIPLE_CLAIMS,
    E_NOTHING_READY,
    E_OFFLINE,
    E_OUTPUT_TRUNCATED,
    E_OVERSIZED,
    E_SCHEMA,
    E_STATE,
    E_UNKNOWN,
    TodoError,
)

MAX_BYTES = 16 * 1024
LIST_DEFAULT_LIMIT = 5
LIST_MAX_LIMIT = 50
SECTION_BUDGET = 6000
TAKE_EXCERPT = 2000
NEEDS_INLINE = 50
MAX_ERROR_CHARS = 2000
TOKENIZER_NAME = "o200k_base"


def count_tokens(text: str) -> tuple[int, str]:
    """Return (tokens, tokenizer). o200k_base when available, else chars/4."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding(TOKENIZER_NAME).encode(text)), TOKENIZER_NAME
    except Exception:
        return max(1, len(text) // 4), "chars/4-fallback"


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _wire(obj: Any) -> str:
    """The transport serialization size checks run against: indented JSON."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _stamp_rev(data: dict[str, Any], rev: str, stale: bool) -> None:
    if stale:
        data.pop("rev", None)
        data["cached_rev"] = rev
        data["stale"] = True


def err(code: str, message: str, recovery: list[str] | None = None, kind: str | None = None) -> dict[str, Any]:
    # Error envelopes are bounded too: echoed inputs and remote chatter are
    # trimmed before they can blow the cap the success path honors.
    if kind is None:
        kind = "error" if code in (E_OFFLINE, E_UNKNOWN, E_STATE) else "gate"
    return {
        "ok": False,
        "code": code,
        "error": _trim(str(message), MAX_ERROR_CHARS),
        "recovery": [_trim(str(step), 500) for step in (recovery or [])],
        "kind": kind,
    }


def _multiple_claims(worker: str, held: str) -> TodoError:
    """The refusal for a worker that already holds a live claim on another task."""
    return TodoError(
        f"worker {worker!r} already holds a live claim on {held!r}; release or finish it first",
        code=E_MULTIPLE_CLAIMS,
        recovery=[
            f"finish or release {held!r} with the generation its take returned",
            "a worker holds one live claim outside any batch and one per active batch, so only a claim in "
            "the same slot blocks this one",
            "agents that share one tractable-mcp server and work the same batch (or no batch) still share "
            "that slot; run each on its own tractable-mcp server with a distinct --actor to claim concurrently",
        ],
    )


MAX_PAYLOAD_FILE_BYTES = 1024 * 1024


def resolve_payload(label: str, inline: dict[str, Any] | None, path: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(payload, None)`` from the inline object or ``path``, else ``(None, error)``; exactly one source is allowed."""

    if inline is not None and path is not None:
        return None, err(E_STATE, f"pass {label} or {label}_path, not both")
    if path is None:
        if inline is None:
            return None, err(E_STATE, f"{label} or {label}_path is required")
        return inline, None
    if not os.path.isabs(path):
        return None, err(E_STATE, f"{label}_path must be an absolute path")
    try:
        size = os.path.getsize(path)
        if size > MAX_PAYLOAD_FILE_BYTES:
            return None, err(E_STATE, f"{label}_path file exceeds {MAX_PAYLOAD_FILE_BYTES} bytes")
        with open(path, "rb") as handle:
            loaded = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return None, err(E_STATE, f"cannot read {label}_path as JSON: {exc}")
    if not isinstance(loaded, dict):
        return None, err(E_STATE, f"{label}_path must contain a JSON object")
    return loaded, None


def _fits(envelope: dict[str, Any]) -> bool:
    return len(_wire(envelope).encode("utf-8")) <= MAX_BYTES


def _public_session_entry(entry: Any) -> Any:
    """Read-path view of a session-history entry.

    Generations authorize only the holder's own take response; republishing
    them — especially the live one — would let a stale same-worker image
    that hits ``E_CLAIM_STALE`` read the rotated generation out of
    ``show_item`` and adopt the claim it was meant to lose. The durable
    log keeps generations for audit; the API never serves them.
    """
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if key != "generation"}


def _query_fingerprint(status: str | None, priority: str | None, text: str | None, ready_only: bool, limit: int) -> str:
    raw = _compact({"s": status, "p": priority, "t": text, "r": ready_only, "l": limit})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _deferral_fingerprint(from_item: str | None, resolution: str | None, limit: int) -> str:
    raw = _compact({"f": from_item, "r": resolution, "l": limit})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def encode_cursor(rev: str, offset: int, fingerprint: str) -> str:
    raw = _compact({"rev": rev, "offset": offset, "q": fingerprint})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, int, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return str(payload["rev"]), int(payload["offset"]), str(payload["q"])
    except Exception as exc:
        raise TodoError(f"invalid cursor: {exc}", code=E_CURSOR_STALE) from exc


@dataclass
class TrackerService:
    """One worker's view of a state branch. Cache lives outside worktrees."""

    ref: git_backend.StateRef
    cache_dir: str | Path
    worker: str
    ttl_hours: float = S.DEFAULT_TTL_HOURS
    session_id: str | None = None
    client_name: str | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        self.worker = S.validate_worker(self.worker)
        if self.session_id is not None:
            self.session_id = S.validate_session_id(self.session_id)
        if self.client_name is not None:
            self.client_name = S.validate_client_name(self.client_name)

    # -- reads --

    def _read(self) -> git_backend.ReadOutcome:
        try:
            return git_backend.read(self.ref, self.cache_dir)
        except TodoError as exc:
            raise exc

    def list_items(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        text: str | None = None,
        ready_only: bool = False,
        limit: int = LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        try:
            now = datetime.now(timezone.utc)
            outcome = self._read()
            rows = S.query_items(
                outcome.snapshot, status=status, priority=priority,
                text=text, ready_only=ready_only, now=now,
            )
            if ready_only:
                rows = [
                    (item_id, entry)
                    for item_id, entry in rows
                    if self._batch_runtime_ready(outcome.snapshot, item_id)
                ]
            total = len(rows)
            limit = max(1, min(int(limit), LIST_MAX_LIMIT))
            fingerprint = _query_fingerprint(status, priority, text, ready_only, limit)
            offset = 0
            if cursor:
                rev, offset, seen = decode_cursor(cursor)
                if rev != outcome.rev or seen != fingerprint:
                    return err(
                        E_CURSOR_STALE,
                        "cursor is stale (state moved) or was made for different filters; "
                        "retry from the first page",
                        recovery=["call list_items without a cursor"],
                    )
            if total == 0 and not cursor:
                if ready_only:
                    return err(E_NOTHING_READY, "no ready tasks", kind="gate")
            page = rows[offset : offset + limit]
            items = []
            for item_id, entry in page:
                runtime_ready = self._batch_runtime_ready(outcome.snapshot, item_id)
                row = {
                    "id": item_id,
                    "title": entry["title"],
                    "priority": entry["priority"],
                    "status": entry["status"],
                    "ready": S.is_ready(item_id, outcome.snapshot, now) and runtime_ready,
                }
                waiting = S.waiting_until(item_id, outcome.snapshot, now)
                if waiting is not None:
                    row["waiting_until"] = waiting
                items.append(row)
            remaining = total - offset - len(page)
            data: dict[str, Any] = {"items": items, "total": total, "limit": limit, "cursor_offset": offset}
            if outcome.stale:
                data["cached_rev"] = outcome.rev
                data["stale"] = True
            else:
                data["rev"] = outcome.rev
            if remaining > 0:
                data["next_cursor"] = encode_cursor(outcome.rev, offset + len(page), fingerprint)
            env: dict[str, Any] = ok(data)
            while items and not _fits(env):
                items.pop()
                data["items"] = items
                data["truncated"] = True
                data.pop("next_cursor", None)
                if offset + len(items) < total:
                    data["next_cursor"] = encode_cursor(outcome.rev, offset + len(items), fingerprint)
                env = ok(data)
            if not _fits(env):
                return err(
                    E_OUTPUT_TRUNCATED,
                    "list response exceeds the byte cap; narrow with status/priority/text filters",
                    recovery=["retry with a text filter or ready_only=true"],
                )
            return env
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

    def _page_ids(self, values: list[str], rev: str, field: str) -> dict[str, Any]:
        # Index-owned collections page too: inline the head, spill the rest.
        if len(values) <= NEEDS_INLINE:
            return {"values": values, "total": len(values)}
        return {
            "values": values[:NEEDS_INLINE],
            "total": len(values),
            "continuation": {"field": field, "offset": NEEDS_INLINE, "budget": SECTION_BUDGET, "rev": rev},
        }

    def show_item(
        self,
        item_id: str,
        *,
        field: str | None = None,
        offset: int = 0,
        budget: int = SECTION_BUDGET,
        rev: str | None = None,
    ) -> dict[str, Any]:
        try:
            now = datetime.now(timezone.utc)
            outcome = self._read()
            snap = outcome.snapshot
            if item_id not in snap.index["items"]:
                return err(E_STATE, f"unknown task {item_id!r}")
            entry = snap.index["items"][item_id]
            detail = snap.details[item_id]
            if field is not None:
                return self._show_section(
                    item_id, snap, outcome.rev, field, offset, budget, rev, stale=outcome.stale,
                )
            needs = list(entry.get("needs", []))
            unmet = [dep for dep in needs if snap.index["items"][dep]["status"] != "done"]
            data: dict[str, Any] = {
                "id": item_id,
                "title": entry["title"],
                "priority": entry["priority"],
                "status": entry["status"],
                "ready": S.is_ready(item_id, snap, now) and self._batch_runtime_ready(snap, item_id),
                "needs": self._page_ids(needs, outcome.rev, "needs"),
                "unmet_needs": self._page_ids(unmet, outcome.rev, "unmet_needs"),
                "unlocks": S.downstream_unlocks(item_id, snap),
                "rev": outcome.rev,
            }
            waiting = S.waiting_until(item_id, snap, now)
            if waiting is not None:
                data["waiting_until"] = waiting
            open_def = S.open_deferrals(snap, item_id)
            if open_def:
                data["open_deferrals"] = sorted(row["id"] for row in open_def)
            if entry.get("claim"):
                claim = entry["claim"]
                data["claim"] = {
                    "worker": claim["worker"],
                    "expires_at": claim["expires_at"],
                    "live": S.claim_is_live(claim, now),
                }
            log = detail.get("sessions", [])
            if isinstance(log, list) and log:
                data["sessions"] = {
                    "total": len(log),
                    "last": _public_session_entry(log[-1]),
                }
                data["sessions_continuation"] = {
                    "field": "sessions", "offset": 0, "budget": SECTION_BUDGET, "rev": outcome.rev,
                }
            sections: dict[str, Any] = {}
            for key in ("description", "acceptance", "links", "context", "legacy", "sessions"):
                if key in detail:
                    sections[key] = self._section_summary(detail[key])
            sections["needs"] = {"total": len(needs)}
            data["sections"] = sections
            # Inline small fields; spill large ones to section reads.
            for key in ("description", "context"):
                value = detail.get(key, "")
                if isinstance(value, str) and len(value.encode("utf-8")) <= SECTION_BUDGET:
                    data[key] = value
                elif isinstance(value, str) and value:
                    data[f"{key}_continuation"] = {
                        "field": key, "offset": 0, "budget": SECTION_BUDGET, "rev": outcome.rev,
                    }
            for key in ("acceptance", "links"):
                value = detail.get(key, [])
                blob = _compact(value)
                if len(blob.encode("utf-8")) <= SECTION_BUDGET:
                    data[key] = value
                elif value:
                    data[f"{key}_continuation"] = {
                        "field": key, "offset": 0, "budget": SECTION_BUDGET, "rev": outcome.rev,
                    }
            if entry["status"] == "dropped" and detail.get("drop_reason") is not None:
                data["drop_reason"] = detail["drop_reason"]
            if detail.get("batch") is not None:
                data["batch"] = detail["batch"]
            if detail.get("prepared") is not None:
                prepared = detail["prepared"]
                data["prepared"] = {
                    "batch_id": prepared["batch_id"],
                    "member_id": prepared["member_id"],
                    "source_worktree": prepared["source_worktree"],
                    "source_revision": prepared["source_revision"],
                    "source_base": prepared["source_base"],
                    "accepted_head": prepared["accepted_head"],
                    "integration_head": prepared["integration_head"],
                    "changed_files": prepared["changed_files"],
                    "verification": {
                        "status": prepared["verification"]["status"],
                        "revision": prepared["verification"]["revision"],
                        "clean": prepared["verification"]["clean"],
                        "suite": prepared["verification"]["suite"],
                    },
                }
            if detail.get("final_evidence") is not None:
                final = detail["final_evidence"]
                data["final_evidence"] = {
                    "status": final["status"],
                    "tree_worktree": final["tree_worktree"],
                    "tree_revision": final["tree_revision"],
                    "scope_hash": final["scope_hash"],
                    "suite": final["suite"],
                    "clean": final["clean"],
                }
            _stamp_rev(data, outcome.rev, outcome.stale)
            env = ok(data)
            if _fits(env):
                return env
            slim: dict[str, Any] = {
                "id": item_id,
                "title": entry["title"],
                "priority": entry["priority"],
                "status": entry["status"],
                "rev": outcome.rev,
                "sections": sections,
                "continuation": "response exceeded the byte cap; read sections with show_item field/offset",
            }
            _stamp_rev(slim, outcome.rev, outcome.stale)
            if entry["status"] == "dropped" and detail.get("drop_reason") is not None:
                slim["drop_reason"] = detail["drop_reason"]
            if _fits(ok(slim)):
                return ok(slim)
            return err(
                E_OVERSIZED,
                f"task {item_id!r} metadata exceeds the byte cap; read one section at a time",
                recovery=[f"show_item id={item_id!r} field='description'"],
            )
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

    def _section_summary(self, value: Any) -> dict[str, Any]:
        blob = value if isinstance(value, str) else _compact(value)
        size = len(blob.encode("utf-8"))
        tokens, tokenizer = count_tokens(blob)
        return {"bytes": size, "tokens": tokens, "tokenizer": tokenizer}

    def _show_section(
        self,
        item_id: str,
        snap: S.Snapshot,
        current_rev: str,
        field: str,
        offset: int,
        budget: int,
        want_rev: str | None,
        *,
        stale: bool = False,
    ) -> dict[str, Any]:
        # Section continuations bind the revision they were issued for: state
        # may have moved since, and splicing versions must be explicit.
        # Offset pages always continue a sequence, so they always require
        # the revision; only a first page (offset 0) may read current.
        if offset > 0 and want_rev is None:
            return err(
                E_CURSOR_STALE,
                "section continuation requires its revision; re-read show_item first",
                recovery=["call show_item without field/offset"],
            )
        if want_rev is not None and want_rev != current_rev:
            return err(
                E_CURSOR_STALE,
                "section revision changed; re-read show_item before continuing",
                recovery=["call show_item without field/offset"],
            )
        entry = snap.index["items"][item_id]
        detail = snap.details[item_id]
        if field in ("needs", "unmet_needs"):
            needs = list(entry.get("needs", []))
            values = needs if field == "needs" else [
                dep for dep in needs if snap.index["items"][dep]["status"] != "done"
            ]
            offset = max(0, int(offset))
            if offset >= len(values) and values:
                return err(E_STATE, f"section offset {offset} is past the end ({len(values)})")
            window = values[offset : offset + NEEDS_INLINE]
            data: dict[str, Any] = {
                "id": item_id, "field": field, "offset": offset,
                "total": len(values), "window": window, "rev": current_rev,
            }
            _stamp_rev(data, current_rev, stale)
            if offset + NEEDS_INLINE < len(values):
                data["continuation"] = {
                    "field": field, "offset": offset + NEEDS_INLINE,
                    "budget": budget, "rev": current_rev,
                }
            env = ok(data)
            if not _fits(env):
                return err(E_OVERSIZED, "section window exceeds the byte cap")
            return env
        if field == "sessions":
            # Entry-window paging over the session log: offsets count
            # entries (not bytes), mirroring the needs/unmet_needs path.
            # Unlike needs, entries vary in size, so a full window can
            # exceed the byte cap: shrink it until it fits, mirroring the
            # list_items truncation loop. A single entry always fits (all
            # identifier fields are bounded), so paging always makes
            # progress and no log is ever unreadable.
            log = detail.get("sessions", [])
            if not isinstance(log, list):
                return err(E_STATE, f"task {item_id!r} has a malformed sessions log")
            offset = max(0, int(offset))
            if offset >= len(log) and log:
                return err(E_STATE, f"section offset {offset} is past the end ({len(log)})")
            window = [_public_session_entry(entry) for entry in log[offset : offset + NEEDS_INLINE]]
            data: dict[str, Any] = {
                "id": item_id, "field": field, "offset": offset,
                "total": len(log), "window": window, "rev": current_rev,
            }
            _stamp_rev(data, current_rev, stale)
            if offset + len(window) < len(log):
                data["continuation"] = {
                    "field": field, "offset": offset + len(window),
                    "budget": budget, "rev": current_rev,
                }
            env = ok(data)
            while len(window) > 1 and not _fits(env):
                window.pop()
                data["window"] = window
                data.pop("continuation", None)
                if offset + len(window) < len(log):
                    data["continuation"] = {
                        "field": field, "offset": offset + len(window),
                        "budget": budget, "rev": current_rev,
                    }
                env = ok(data)
            if not _fits(env):
                return err(E_OVERSIZED, "section window exceeds the byte cap")
            return env
        if field not in detail:
            return err(E_STATE, f"task {item_id!r} has no section {field!r}")
        budget = max(1, min(int(budget), MAX_BYTES))
        offset = max(0, int(offset))
        value = detail[field]
        blob = value if isinstance(value, str) else _compact(value)
        total = len(blob)
        if offset >= total:
            return err(E_STATE, f"section offset {offset} is past the end ({total})")
        window = blob[offset : offset + budget]
        data = {
            "id": item_id, "field": field, "offset": offset,
            "total": total, "window": window, "rev": current_rev,
        }
        _stamp_rev(data, current_rev, stale)
        if offset + budget < total:
            data["continuation"] = {
                "field": field, "offset": offset + budget, "budget": budget, "rev": current_rev,
            }
        env = ok(data)
        if not _fits(env):
            return err(
                E_OVERSIZED,
                "section window exceeds the byte cap; retry with a smaller budget",
                recovery=["retry with a smaller budget"],
            )
        return env

    def list_deferrals(
        self,
        *,
        from_item: str | None = None,
        resolution: str | None = None,
        limit: int = LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List deferral rows: open by default, `all` for every resolution."""
        try:
            outcome = self._read()
            try:
                rows = S.query_deferrals(outcome.snapshot, from_item=from_item, resolution=resolution)
            except TodoError as exc:
                return err(exc.code or E_STATE, str(exc))
            total = len(rows)
            limit = max(1, min(int(limit), LIST_MAX_LIMIT))
            fingerprint = _deferral_fingerprint(from_item, resolution, limit)
            offset = 0
            if cursor:
                rev, offset, seen = decode_cursor(cursor)
                if rev != outcome.rev or seen != fingerprint:
                    return err(
                        E_CURSOR_STALE,
                        "cursor is stale (state moved) or was made for different filters; "
                        "retry from the first page",
                        recovery=["call list_deferrals without a cursor"],
                    )
            page = []
            for row in rows[offset : offset + limit]:
                entry: dict[str, Any] = {
                    "id": row["id"],
                    "from_item": row["from_item"],
                    "summary": row["summary"],
                    "reason": row["reason"],
                    "resolution": row.get("resolution", "open"),
                }
                if row.get("resolved_item") is not None:
                    entry["resolved_item"] = row["resolved_item"]
                if row.get("resolved_reason") is not None:
                    entry["resolved_reason"] = row["resolved_reason"]
                page.append(entry)
            data: dict[str, Any] = {"deferrals": page, "total": total, "limit": limit, "cursor_offset": offset}
            if outcome.stale:
                data["cached_rev"] = outcome.rev
                data["stale"] = True
            else:
                data["rev"] = outcome.rev
            remaining = total - offset - len(page)
            if remaining > 0:
                data["next_cursor"] = encode_cursor(outcome.rev, offset + len(page), fingerprint)
            env = ok(data)
            if not _fits(env):
                return err(
                    E_OUTPUT_TRUNCATED,
                    "deferral list exceeds the byte cap; narrow with from_item/resolution filters",
                    recovery=["retry with from_item set"],
                )
            return env
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

    # -- mutations --

    def _record_session(
        self, snap: S.Snapshot, item_id: str, op: str, op_id: str,
        generation: str | None = None,
        reason: str | None = None,
        displaced: str | None = None,
    ) -> None:
        # History annotates; it never gates. Constructions without a
        # session (legacy callers, older tests) skip recording and the
        # mutation still succeeds.
        if self.session_id is None:
            return
        S.record_session(
            snap, item_id, actor=self.worker, session_id=self.session_id,
            client=self.client_name, op=op, op_id=op_id, generation=generation,
            reason=reason, displaced=displaced,
        )

    def _check_take_readiness(self, snap: S.Snapshot, item_id: str, now: datetime) -> None:
        """Batch/dependency readiness shared by take and takeover.

        Both acquire a live claim; both must refuse unready batch members
        identically, or takeover would bypass the guards take enforces.
        Both also honor the not_before hold: a held task is refused
        rather than taken early.
        """
        # A held task is refused rather than taken early: taking and
        # finishing it would close the task before its wait ends.
        waiting = S.waiting_until(item_id, snap, now)
        if waiting is not None:
            raise TodoError(
                f"task {item_id!r} is waiting until {waiting}; "
                "to override, clear it with update_item not_before=\"\"",
                code=E_NOTHING_READY,
            )
        detail = snap.details.get(item_id, {})
        batch_meta = detail.get("batch") if isinstance(detail, dict) else None
        if isinstance(batch_meta, dict):
            entry = snap.index["items"].get(item_id, {})
            implementation_deps = set(batch_meta.get("implementation_dependencies", []))
            has_prepared_edge = any(
                dep in implementation_deps and snap.index["items"][dep]["status"] != "done"
                for dep in entry.get("needs", [])
            )
            if has_prepared_edge and (not S.is_ready(item_id, snap, now) or not self._batch_runtime_ready(snap, item_id)):
                raise TodoError(f"task {item_id!r} is not ready at the current integration head", code=E_NOTHING_READY)

    def _mutate(
        self, op: str, summary: str,
        apply: Callable[[S.Snapshot, str], dict[str, Any]],
    ) -> dict[str, Any]:
        op_id = self.operation_id or git_backend.new_op_id()

        def bridged(snap: S.Snapshot) -> dict[str, Any]:
            return apply(snap, op_id)

        try:
            result = git_backend.mutate(
                self.ref, op=op, summary=summary, worker=self.worker, op_id=op_id, apply=bridged,
                session=self.session_id, cache_dir=self.cache_dir, replay=bool(self.operation_id),
            )
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc), recovery=exc.recovery)
        if result.ok:
            ack = {"id": result.ack.get("id"), "rev": result.sha, **{k: v for k, v in result.ack.items() if k not in ("id", "_no_write")}}
            # Section continuations minted pre-commit learn their revision
            # here, at any nesting depth (take nests needs continuations).
            def stamp(node: Any) -> None:
                if isinstance(node, dict):
                    if {"field", "offset"} <= set(node):
                        node.setdefault("rev", result.sha)
                    for child in node.values():
                        stamp(child)
                elif isinstance(node, list):
                    for child in node:
                        stamp(child)

            stamp(ack)
            env = ok(ack)
            if not _fits(env):
                return err(E_OUTPUT_TRUNCATED, "acknowledgement exceeded the byte cap")
            return env
        if result.outcome == "unknown":
            return err(
                E_UNKNOWN,
                f"{result.error} (operation ID {result.op_id})",
                recovery=[
                    f"retry the same call with operation_id={result.op_id}: if it landed, the original "
                    "outcome is returned and nothing is applied twice",
                    f"or look the operation up with `tractable recover --op-id {result.op_id}`",
                ],
            )
        if result.outcome == "offline":
            return err(E_OFFLINE, result.error or "remote unreachable", recovery=["retry when the remote is reachable"])
        return err(result.code or E_STATE, result.error or "mutation failed", recovery=result.recovery or None)

    @staticmethod
    def _claim_slot(snap: S.Snapshot, item_id: str) -> str | None:
        batch_id, _ = S.active_batch_of(snap, item_id)
        return batch_id

    def _held_claim(self, snap: S.Snapshot, now: datetime, item_id: str) -> str | None:
        slot = self._claim_slot(snap, item_id)
        for held_id, entry in snap.index["items"].items():
            claim = entry.get("claim")
            if (
                claim
                and claim.get("worker") == self.worker
                and S.claim_is_live(claim, now)
                and self._claim_slot(snap, held_id) == slot
            ):
                return held_id
        return None

    @staticmethod
    def _validate_source_checkout(
        source_worktree: str,
        source_revision: str,
        *,
        expected_branch: str | None = None,
        expected_repository: str | None = None,
    ) -> str:
        """Prove the receipt names a clean checkout at the recorded revision."""
        path = Path(source_worktree).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise TodoError("source_worktree must be an existing absolute directory", code=E_STATE)
        path = path.resolve()
        try:
            root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], cwd=path, check=True,
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=path, check=True,
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=path,
                check=False, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            common_dir = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path,
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"], cwd=path,
                check=False, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=path,
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise TodoError(f"source_worktree is not a usable Git checkout: {exc}", code=E_STATE) from exc
        if Path(root).resolve() != path:
            raise TodoError("source_worktree does not resolve to the checkout root", code=E_STATE)
        if head != source_revision:
            raise TodoError("source checkout HEAD differs from source_revision", code=E_STATE)
        if dirty:
            raise TodoError("source checkout is dirty; prepare requires a clean exact checkout", code=E_STATE)
        if expected_branch is not None and branch != expected_branch:
            raise TodoError("checkout branch differs from the registered integration branch", code=E_STATE)
        if expected_repository and expected_repository.startswith("/"):
            candidates = {str(path), str(Path(common_dir).resolve()), str(Path(common_dir).resolve().parent)}
            if str(Path(expected_repository).resolve()) not in candidates:
                raise TodoError("checkout belongs to a different registered repository", code=E_STATE)
        elif expected_repository:
            if not remote or remote.rstrip("/") != expected_repository.rstrip("/"):
                raise TodoError("checkout origin differs from the registered repository", code=E_STATE)
        return str(path)

    @staticmethod
    def _changed_paths(path: str, base: str, head: str) -> list[str]:
        try:
            output = subprocess.run(
                ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}..{head}"], cwd=path,
                check=True, capture_output=True, timeout=30,
            ).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise TodoError(f"cannot inspect prepared member range: {exc}", code=E_STATE) from exc
        return sorted({os.fsdecode(value) for value in output.split(b"\0") if value})

    @staticmethod
    def _is_ancestor(path: str, ancestor: str, descendant: str) -> bool:
        try:
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                cwd=path, check=True, capture_output=True, text=True, timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 1:
                return False
            raise TodoError(f"cannot inspect ancestry: {exc.stderr.strip()}", code=E_STATE) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TodoError(f"cannot inspect ancestry: {exc}", code=E_STATE) from exc
        return True

    @staticmethod
    def _git_out(path: str, *args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=path, check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise TodoError(f"git {args[0]} failed: {exc}", code=E_STATE) from exc

    def _batch_runtime_ready(self, snap: S.Snapshot, item_id: str) -> bool:
        """Check live Git state for prepared edges; ordinary work stays local-only."""
        detail = snap.details.get(item_id, {})
        batch_meta = detail.get("batch")
        if not isinstance(batch_meta, dict):
            return True
        entry = snap.index["items"].get(item_id, {})
        implementation_deps = set(batch_meta.get("implementation_dependencies", []))
        if not any(dep in implementation_deps and snap.index["items"][dep]["status"] != "done" for dep in entry.get("needs", [])):
            return True
        batch = snap.index.get("batches", {}).get(batch_meta.get("batch_id"))
        if not isinstance(batch, dict):
            return False
        try:
            integration_worktree = self._validate_source_checkout(
                batch["integration_worktree"], batch["integration_head"],
                expected_branch=batch["integration_branch"], expected_repository=batch["repository"],
            )
            for dependency in implementation_deps:
                if snap.index["items"].get(dependency, {}).get("status") == "done":
                    continue
                dep_detail = snap.details.get(dependency, {})
                receipt = dep_detail.get("prepared")
                if not isinstance(receipt, dict):
                    return False
                if not self._is_ancestor(integration_worktree, receipt["accepted_head"], batch["integration_head"]):
                    return False
        except (KeyError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, TodoError):
            return False
        return True

    def _validate_final_checkout(self, snap: S.Snapshot, item_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """Re-prove the registered integration checkout on every retry."""
        if item_id not in snap.details or item_id not in snap.index["items"]:
            raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
        if not isinstance(snap.details[item_id].get("batch"), dict):
            raise TodoError("final-tree evidence requires a registered batch", code=E_FINAL_EVIDENCE)
        batch_id = snap.details[item_id]["batch"]["batch_id"]
        batch = snap.index["batches"][batch_id]
        if S.batch_schema_of(batch) != S.BATCH_SCHEMA:
            raise TodoError(
                f"batch {batch_id!r} predates batch v2; finish it with the client that registered it or abort it",
                code=E_SCHEMA,
            )
        checked = S._validate_batch_final_evidence(snap, item_id, S._validate_final_evidence(item_id, evidence))
        if checked["tree_worktree"] != batch["integration_worktree"]:
            raise TodoError("final tree is not the registered integration worktree", code=E_STATE)
        clean = self._validate_source_checkout(
            checked["tree_worktree"], checked["tree_revision"],
            expected_branch=batch["integration_branch"], expected_repository=batch["repository"],
        )
        ancestors = [(entry["head"], f"recorded integration head {entry['seq']}") for entry in batch["integration_log"]]
        ancestors += [(head, f"prepared member {member}") for member, head in checked["member_heads"].items()]
        for revision, label in ancestors:
            try:
                contained = self._is_ancestor(clean, revision, checked["tree_revision"])
            except TodoError as exc:
                raise TodoError(
                    f"final tree does not contain {label}: {exc}", code=E_FINAL_EVIDENCE,
                    recovery=["merge every recorded head into the integration worktree and rerun the final suite"],
                ) from exc
            if not contained:
                raise TodoError(
                    f"final tree does not contain {label}", code=E_FINAL_EVIDENCE,
                    recovery=["merge every recorded head into the integration worktree and rerun the final suite"],
                )
        actual = set(self._changed_paths(clean, batch["start_head"], checked["tree_revision"]))
        expected = actual - S.excluded_paths(snap, batch_id)
        if expected != set(checked["changed_files"]):
            raise TodoError("final evidence changed_files drifted from the registered integration tree", code=E_FINAL_EVIDENCE)
        return checked

    def create_item(self, item_id: str, title: str, **kwargs: Any) -> dict[str, Any]:
        # Judge a not_before hold against the request time, not the time of
        # a retry after a competing push.
        requested = datetime.now(timezone.utc)

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            S.op_create(snap, item_id=item_id, title=title, now=requested, **kwargs)
            self._record_session(snap, item_id, "create", op_id)
            return {"id": item_id, "status": "open"}

        return self._mutate("create", f"add {item_id}", apply)

    def register_batch(self, **kwargs: Any) -> dict[str, Any]:
        """Register the immutable batch contract before member work starts."""
        kwargs = dict(kwargs)
        kwargs["owner"] = self.worker
        try:
            self._validate_source_checkout(
                str(kwargs["integration_worktree"]), str(kwargs["start_head"]),
                expected_branch=str(kwargs["integration_branch"]),
                expected_repository=str(kwargs["repository"]),
            )
        except (KeyError, TodoError) as exc:
            return err(getattr(exc, "code", None) or E_STATE, str(exc))

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            self._validate_source_checkout(
                str(kwargs["integration_worktree"]), str(kwargs["start_head"]),
                expected_branch=str(kwargs["integration_branch"]),
                expected_repository=str(kwargs["repository"]),
            )
            return S.op_register_batch(snap, **kwargs)

        return self._mutate("register_batch", f"{self.worker} registers batch {kwargs['batch_id']}", apply)

    def abort_batch(self, batch_id: str, owner_generation: str) -> dict[str, Any]:
        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            out = S.op_abort_batch(snap, batch_id, self.worker, owner_generation)
            # A repeat abort changes nothing: keep the legacy refusal by
            # recording nothing, mirroring the no-op update path.
            if not out.get("idempotent"):
                for member in out.get("affected", []):
                    self._record_session(snap, member, "abort", op_id)
            return out

        return self._mutate("abort_batch", f"{self.worker} aborts batch {batch_id}", apply)

    def bind_batch_pr(
        self, batch_id: str, owner_generation: str, *, pr_repository: str, number: int, node_id: str,
        head: str, expected_previous_head: str | None = None,
    ) -> dict[str, Any]:
        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            return S.op_bind_batch_pr(
                snap, batch_id, self.worker, owner_generation, pr_repository=pr_repository,
                number=number, node_id=node_id, head=head,
                expected_previous_head=expected_previous_head, op_id=op_id,
            )

        return self._mutate("bind_batch_pr", f"{self.worker} binds final PR for {batch_id}", apply)

    def _verify_advance(
        self, batch: dict[str, Any], expected_head: str, head: str, reason: str, changed_files: list[str],
    ) -> str | None:
        """Prove an advance against the real integration checkout; returns the merged parent."""
        worktree = self._validate_source_checkout(
            str(batch["integration_worktree"]), head,
            expected_branch=str(batch["integration_branch"]), expected_repository=str(batch["repository"]),
        )
        if expected_head == head or not self._is_ancestor(worktree, expected_head, head):
            raise TodoError(
                "new integration head does not descend from the expected head", code=E_BATCH_HEAD,
                recovery=["commit the advance on top of the registered integration head"],
            )
        if self._changed_paths(worktree, expected_head, head) != sorted(set(changed_files)):
            raise TodoError(
                "changed_files differ from the advance's actual diff", code=E_STATE,
                recovery=["pass changed_files equal to the git diff between expected_head and head"],
            )
        if reason != "develop-merge":
            return None
        develop_ref = batch.get("develop_ref")
        if develop_ref is None:
            raise TodoError(
                f"batch {batch['integration_branch']!r} was registered without a develop_ref", code=E_BATCH_HEAD,
                recovery=["record the change as a fix advance"],
            )
        parents = self._git_out(worktree, "rev-list", "--parents", "-n", "1", head).split()[1:]
        if len(parents) != 2 or parents[0] != expected_head:
            raise TodoError(
                "a develop merge must be a two-parent merge whose first parent is the expected head",
                code=E_BATCH_HEAD, recovery=["merge with --no-ff"],
            )
        merged_parent = parents[1]
        try:
            self._git_out(worktree, "rev-parse", "--verify", "--quiet", f"{develop_ref}^{{commit}}")
        except TodoError as exc:
            raise TodoError(
                f"develop_ref {develop_ref!r} does not resolve in the integration worktree", code=E_BATCH_HEAD,
                recovery=["fetch the develop ref into the integration worktree and retry"],
            ) from exc
        if not self._is_ancestor(worktree, merged_parent, develop_ref):
            raise TodoError(
                "merged parent is not an ancestor of the registered develop_ref", code=E_BATCH_HEAD,
                recovery=["merge a commit that is already on the registered develop_ref"],
            )
        base = self._git_out(worktree, "merge-base", expected_head, merged_parent)
        side = set(self._changed_paths(worktree, base, merged_parent))
        if not set(changed_files) <= side:
            raise TodoError(
                "merge introduces non-develop content", code=E_BATCH_HEAD,
                recovery=["split member changes into a fix advance"],
            )
        return merged_parent

    def advance_batch_head(
        self, batch_id: str, owner_generation: str, *, expected_head: str, head: str, reason: str,
        changed_files: list[str],
    ) -> dict[str, Any]:
        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            batch = snap.index.get("batches", {}).get(batch_id)
            merged_parent = None
            if (
                isinstance(batch, dict)
                and S.batch_schema_of(batch) == S.BATCH_SCHEMA
                and batch["owner"] == self.worker
                and batch["owner_generation"] == owner_generation
                and batch["integration_head"] == expected_head
                and batch.get("lifecycle", "active") == "active"
            ):
                merged_parent = self._verify_advance(batch, expected_head, head, reason, list(changed_files))
            return S.op_advance_batch_head(
                snap, batch_id, self.worker, owner_generation, expected_head=expected_head, head=head,
                reason=reason, changed_files=changed_files, merged_parent=merged_parent, op_id=op_id,
            )

        return self._mutate("advance_batch_head", f"{self.worker} advances {batch_id} ({reason})", apply)

    def _verify_relocation(
        self,
        snap: S.Snapshot,
        batch: dict[str, Any],
        expected_worktree: str | None,
        expected_head: str | None,
        new_worktree: str | None,
    ) -> str:
        if (
            not isinstance(expected_worktree, str)
            or not isinstance(expected_head, str)
            or not isinstance(new_worktree, str)
            or not expected_worktree
            or not expected_head
            or not new_worktree
            or not os.path.isabs(expected_worktree)
            or not os.path.isabs(new_worktree)
        ):
            raise TodoError(
                "relocation requires all of expected_integration_worktree, expected_integration_head, "
                "and integration_worktree as absolute paths / full revisions",
                code=E_STATE,
                recovery=["pass all three relocation parameters as absolute paths and a full revision"],
            )
        try:
            S._validate_revision(expected_head, "expected_integration_head")
        except TodoError as exc:
            raise TodoError(str(exc), code=E_STATE, recovery=["pass a full 40-character hex revision"]) from exc

        if expected_worktree != batch.get("integration_worktree"):
            raise TodoError(
                f"expected integration worktree {expected_worktree!r} does not match current registered worktree",
                code=E_CONFLICT,
                recovery=["re-read the batch with show_batch"],
            )
        if expected_head != batch.get("integration_head"):
            raise TodoError(
                f"expected integration head {expected_head!r} does not match current registered head",
                code=E_CONFLICT,
                recovery=["re-read the batch with show_batch"],
            )

        clean_dest = self._validate_source_checkout(
            new_worktree,
            expected_head,
            expected_branch=str(batch["integration_branch"]),
            expected_repository=str(batch["repository"]),
        )
        ancestors = [(batch["start_head"], "batch start_head")]
        ancestors += [
            (entry["head"], f"recorded integration head {entry['seq']}")
            for entry in batch.get("integration_log", [])
        ]
        for member in batch.get("members", []):
            receipt = snap.details.get(member, {}).get("prepared")
            if isinstance(receipt, dict) and receipt.get("accepted_head"):
                ancestors.append((receipt["accepted_head"], f"prepared member {member} accepted head"))

        for revision, label in ancestors:
            try:
                contained = self._is_ancestor(clean_dest, revision, expected_head)
            except TodoError as exc:
                raise TodoError(
                    f"relocated integration worktree does not contain {label}: {exc}", code=E_STATE,
                    recovery=["fetch all recorded heads and member branches into the destination worktree and retry"],
                ) from exc
            if not contained:
                raise TodoError(
                    f"relocated integration worktree does not contain {label}", code=E_STATE,
                    recovery=["fetch all recorded heads and member branches into the destination worktree and retry"],
                )
        return clean_dest

    def recover_batch_owner(
        self, batch_id: str, expected_owner: str, expected_owner_generation: str,
        new_owner_generation: str, reason: str,
        *,
        expected_integration_worktree: str | None = None,
        expected_integration_head: str | None = None,
        integration_worktree: str | None = None,
    ) -> dict[str, Any]:
        if self.session_id is None:
            return err(E_STATE, "recover_batch_owner requires a session-attributed caller")

        relocation_requested = (
            expected_integration_worktree is not None
            or expected_integration_head is not None
            or integration_worktree is not None
        )

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            batch = snap.index.get("batches", {}).get(batch_id)
            if (
                isinstance(batch, dict)
                and S.batch_schema_of(batch) == S.BATCH_SCHEMA
                and batch.get("lifecycle", "active") == "active"
                and relocation_requested
            ):
                is_idempotent = (
                    batch["owner"] == self.worker
                    and batch["owner_generation"] == new_owner_generation
                    and bool(batch.get("recoveries"))
                    and batch["recoveries"][-1].get("previous_owner") == expected_owner
                )
                if not is_idempotent:
                    self._verify_relocation(
                        snap, batch, expected_integration_worktree, expected_integration_head, integration_worktree,
                    )
            out = S.op_recover_batch_owner(
                snap, batch_id, self.worker, expected_owner=expected_owner,
                expected_owner_generation=expected_owner_generation,
                new_owner_generation=new_owner_generation, reason=reason,
                expected_integration_worktree=expected_integration_worktree,
                expected_integration_head=expected_integration_head,
                integration_worktree=integration_worktree,
                session_id=self.session_id, client=self.client_name, op_id=op_id,
            )
            if not out["idempotent"]:
                for member in out["recovered_members"]:
                    if member in snap.index["items"]:
                        self._record_session(
                            snap, member, "recover", op_id, reason=reason, displaced=out["previous_owner"],
                        )
                out.pop("previous_owner")
            return out

        return self._mutate(
            "recover_batch_owner",
            f"{self.worker} recovers batch {batch_id} from {expected_owner}: {reason}", apply,
        )

    BATCH_SECTIONS = (
        "members", "scope", "receipts", "integration_log", "acceptance_required",
        "recoveries", "final_pr_history", "final_evidence",
    )

    @staticmethod
    def _batch_section(snap: S.Snapshot, batch_id: str, name: str) -> Any:
        batch = snap.index["batches"][batch_id]
        details = snap.details
        if name == "members":
            rows = []
            for member in batch["members"]:
                entry = snap.index["items"].get(member, {})
                receipt = details.get(member, {}).get("prepared")
                claim = entry.get("claim")
                rows.append({
                    "id": member,
                    "status": entry.get("status"),
                    "claim_worker": claim["worker"] if claim and S.claim_is_live(claim) else None,
                    "prepared": isinstance(receipt, dict),
                    "receipt_schema": receipt.get("schema") if isinstance(receipt, dict) else None,
                    "accepted_head": receipt.get("accepted_head") if isinstance(receipt, dict) else None,
                    "source_base": receipt.get("source_base") if isinstance(receipt, dict) else None,
                    "integration_head": receipt.get("integration_head") if isinstance(receipt, dict) else None,
                    "integration_seq": receipt.get("integration_seq") if isinstance(receipt, dict) else None,
                    "changed_files_count": len(receipt["changed_files"]) if isinstance(receipt, dict) else None,
                })
            return rows
        if name == "scope":
            return batch["scope"]
        if name == "receipts":
            return {
                m: {k: v for k, v in details[m]["prepared"].items() if k != "owner_generation"}
                for m in batch["members"]
                if "prepared" in details.get(m, {})
            }
        if name == "integration_log":
            return batch.get("integration_log", [])
        if name == "acceptance_required":
            return S.acceptance_required(snap, batch_id)
        if name == "recoveries":
            return batch.get("recoveries", [])
        if name == "final_pr_history":
            return batch.get("final_pr_history", [])
        return {m: details[m]["final_evidence"] for m in batch["members"] if "final_evidence" in details.get(m, {})}

    def show_batch(
        self,
        batch_id: str,
        *,
        field: str | None = None,
        offset: int = 0,
        budget: int = SECTION_BUDGET,
        rev: str | None = None,
    ) -> dict[str, Any]:
        """Read one batch record: summary, or one bounded window of a section.

        Never serves ``owner_generation``.
        """
        try:
            outcome = self._read()
            snap = outcome.snapshot
            batch = snap.index.get("batches", {}).get(batch_id)
            if batch is None:
                return err(E_STATE, f"unknown batch {batch_id!r}")
            current_rev = outcome.rev
            if rev is not None and rev != current_rev:
                return err(
                    E_CURSOR_STALE, "revision changed; re-read show_batch before continuing",
                    recovery=["call show_batch without field/offset"],
                )
            if field is not None:
                if field not in self.BATCH_SECTIONS:
                    return err(E_STATE, f"batch {batch_id!r} has no section {field!r}")
                if offset > 0 and rev is None:
                    return err(
                        E_CURSOR_STALE, "section continuation requires its revision; re-read show_batch first",
                        recovery=["call show_batch without field/offset"],
                    )
                blob = _compact(self._batch_section(snap, batch_id, field))
                budget = max(1, min(int(budget), MAX_BYTES))
                offset = max(0, int(offset))
                if offset >= len(blob) and blob != "":
                    return err(E_STATE, f"section offset {offset} is past the end ({len(blob)})")
                data: dict[str, Any] = {
                    "batch_id": batch_id, "field": field, "offset": offset, "total": len(blob),
                    "window": blob[offset : offset + budget], "rev": current_rev,
                }
                _stamp_rev(data, current_rev, outcome.stale)
                if offset + budget < len(blob):
                    data["continuation"] = {
                        "field": field, "offset": offset + budget, "budget": budget, "rev": current_rev,
                    }
                env = ok(data)
                if not _fits(env):
                    return err(
                        E_OVERSIZED, "section window exceeds the byte cap; retry with a smaller budget",
                        recovery=["retry with a smaller budget"],
                    )
                return env
            from . import BATCH_CAPABILITY

            summary: dict[str, Any] = {
                "batch_id": batch_id,
                "lifecycle": batch.get("lifecycle", "active"),
                "batch_schema": S.batch_schema_of(batch),
                "project_id": batch["project_id"],
                "repository": batch["repository"],
                "integration_branch": batch["integration_branch"],
                "integration_worktree": batch["integration_worktree"],
                "start_head": batch["start_head"],
                "integration_head": batch.get("integration_head"),
                "scope_hash": batch["scope_hash"],
                "delivery_boundary": batch["delivery_boundary"],
                "terminal_outcome": batch["terminal_outcome"],
                "owner": batch["owner"],
                "owner_epoch": batch.get("owner_epoch"),
                "develop_ref": batch.get("develop_ref"),
                "final_pr": batch.get("final_pr"),
                "store": {"schema_version": snap.index["schema_version"]},
                "capability": BATCH_CAPABILITY,
                "rev": current_rev,
            }
            _stamp_rev(summary, current_rev, outcome.stale)
            sections: dict[str, Any] = {}
            values = {name: self._batch_section(snap, batch_id, name) for name in self.BATCH_SECTIONS}
            for name in self.BATCH_SECTIONS:
                sections[name] = {"bytes": len(_compact(values[name]).encode("utf-8")), "items": len(values[name])}
            summary["sections"] = sections
            for name in self.BATCH_SECTIONS:
                sections[name]["continuation"] = {
                    "field": name, "offset": 0, "budget": SECTION_BUDGET, "rev": current_rev,
                }
            for name in self.BATCH_SECTIONS:
                stub = sections[name].pop("continuation")
                trial = dict(summary)
                trial[name] = values[name]
                if _fits(ok(trial)):
                    summary = trial
                else:
                    sections[name]["continuation"] = stub
            env = ok(summary)
            if not _fits(env):
                return err(E_OVERSIZED, f"batch {batch_id!r} summary exceeds the byte cap")
            return env
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

    UPDATE_GUARDED_FIELDS = ("title", "priority", "status", "needs")

    def update_item(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        guarded = {key: kwargs[key] for key in self.UPDATE_GUARDED_FIELDS if key in kwargs}
        detail_guarded = {
            key: kwargs[key]
            for key in ("description", "acceptance", "links", "context", "batch", "not_before")
            if key in kwargs
        }
        pre_image: dict[str, Any] | None = None
        requested = datetime.now(timezone.utc)

        def snapshot_pre_image(snap: S.Snapshot) -> dict[str, Any]:
            entry = snap.index["items"][item_id]
            detail = snap.details[item_id]
            image = {key: json.loads(json.dumps(entry.get(key))) for key in guarded}
            for key in detail_guarded:
                image[f"detail:{key}"] = json.loads(json.dumps(detail.get(key)))
            return image

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            nonlocal pre_image
            if item_id not in snap.index["items"]:
                raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
            if pre_image is None:
                # First attempt: record what the touched fields look like.
                pre_image = snapshot_pre_image(snap)
            else:
                # Retry after a competing push: the fields we intend to
                # write must be untouched since our first attempt, or this
                # retry would silently overwrite the winner.
                current = snapshot_pre_image(snap)
                for key, old in pre_image.items():
                    if current.get(key) != old:
                        raise TodoError(
                            f"conflict: {key} on {item_id!r} changed concurrently; "
                            "re-read and retry deliberately",
                            code=E_CONFLICT,
                        )
            out = S.op_update(snap, item_id, now=requested, **kwargs)
            # A content-identical update changes nothing: recording it would
            # turn the legacy "no state change" refusal into a commit, with
            # the outcome depending on whether a session is attached.
            if out.get("changed"):
                self._record_session(snap, item_id, "update", op_id)
            # Downstream members whose receipts this edit invalidated get
            # their own entries under the same op_id, so one commit links
            # the whole cascade.
            for dependent in out.get("invalidated", []):
                self._record_session(snap, dependent, "invalidate", op_id)
            return out

        return self._mutate("update", f"edit {item_id}", apply)

    def take(self, item_id: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            held = self._held_claim(snap, now, item_id)
            if held is not None and held != item_id:
                raise _multiple_claims(worker, held)
            self._check_take_readiness(snap, item_id, now)
            took = S.op_take(snap, item_id, worker, ttl_hours=self.ttl_hours)
            self._record_session(
                snap, item_id, "take", op_id, generation=took["claim"]["generation"]
            )
            entry = snap.index["items"][item_id]
            ack: dict[str, Any] = {
                "id": item_id,
                "status": "active",
                "title": entry["title"],
                "priority": entry["priority"],
                "claim": {
                    "generation": took["claim"]["generation"],
                    "expires_at": took["claim"]["expires_at"],
                },
            }
            return self._claim_ack(snap, item_id, ack)

        return self._mutate("take", f"{worker} takes {item_id}", apply)

    def _claim_ack(self, snap: S.Snapshot, item_id: str, ack: dict[str, Any]) -> dict[str, Any]:
        """Attach working context to a claim ack without breaking the cap.

        Shared by take and takeover: both hand a worker a live claim it
        must immediately work from. The claim is already committed, so the
        minimal ack — identity, claim, revision — always survives: context
        is trimmed to fit before publication, never after.
        """
        entry = snap.index["items"][item_id]
        detail = snap.details[item_id]
        needs = list(entry.get("needs", []))
        unmet = [d for d in needs if snap.index["items"][d]["status"] != "done"]
        description = detail.get("description", "")
        context: dict[str, Any] = {}
        if len(needs) <= NEEDS_INLINE:
            context["needs"] = needs
            context["unmet_needs"] = unmet
        else:
            context["needs"] = {
                "values": needs[:NEEDS_INLINE],
                "total": len(needs),
                "continuation": {"field": "needs", "offset": NEEDS_INLINE, "budget": SECTION_BUDGET},
            }
            context["unmet_needs"] = {
                "values": unmet[:NEEDS_INLINE],
                "total": len(unmet),
                "continuation": {"field": "unmet_needs", "offset": NEEDS_INLINE, "budget": SECTION_BUDGET},
            }
        excerpt = description[:TAKE_EXCERPT]
        while excerpt and not _fits(ok({**ack, **context, "description_excerpt": excerpt})):
            excerpt = excerpt[: len(excerpt) // 2]
        context["description_excerpt"] = excerpt
        if len(description) > len(excerpt):
            context["description_continuation"] = {
                "field": "description",
                "offset": len(excerpt),
                "budget": SECTION_BUDGET,
            }
        # Size against the post-commit envelope: _mutate injects the
        # 40-char revision afterwards, so a boundary fit here must not
        # become an overflow there.
        def fits_published(candidate: dict[str, Any]) -> bool:
            return _fits(ok({"rev": "0" * 40, **candidate}))

        candidate = {**ack, **context}
        if fits_published(candidate):
            return candidate
        # Degenerate fallback: context that still overflows is dropped
        # rather than stranding the worker without its generation.
        del context["description_excerpt"]
        context["description_continuation"] = {"field": "description", "offset": 0, "budget": SECTION_BUDGET}
        candidate = {**ack, **context}
        if fits_published(candidate):
            return candidate
        return ack

    def takeover(self, item_id: str, expected_holder: str, reason: str) -> dict[str, Any]:
        """Take over a live foreign claim whose session cannot resume.

        Authority transfer, not re-adoption: the caller names the exact
        holder it inspected (`expected_holder`) and asserts why that
        session is dead (`reason`). The full claim image is pinned on the
        first attempt so a renewal or handover under retry fails
        `E_CONFLICT` instead of stealing the replacement claim. Unlike
        every other mutation, history here gates: without a session the
        transfer would be unattributed, so sessionless callers are
        refused instead of recorded-skipped.
        """
        worker = self.worker
        if self.session_id is None:
            return err(E_STATE, "takeover requires a session-attributed caller")
        pre_claim: dict[str, Any] | None = None
        seen_claim = False

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            nonlocal pre_claim, seen_claim
            now = datetime.now(timezone.utc)
            held = self._held_claim(snap, now, item_id)
            if held is not None and held != item_id:
                raise _multiple_claims(worker, held)
            if item_id not in snap.index["items"]:
                raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
            current = snap.index["items"][item_id].get("claim")
            if not seen_claim:
                # First attempt: pin the exact claim under transfer.
                seen_claim = True
                pre_claim = json.loads(json.dumps(current))
            elif current != pre_claim:
                # Renewal, release, or handover moved under us: the
                # inspected claim is gone, so there is nothing audited
                # left to transfer. Re-read and decide deliberately.
                raise TodoError(
                    f"claim on {item_id!r} changed concurrently; re-read with show and retry",
                    code=E_CONFLICT,
                )
            self._check_take_readiness(snap, item_id, now)
            took = S.op_takeover(
                snap, item_id, worker, expected_holder, reason, ttl_hours=self.ttl_hours,
            )
            self._record_session(
                snap, item_id, "takeover", op_id, generation=took["claim"]["generation"],
                reason=reason, displaced=took["displaced"],
            )
            entry = snap.index["items"][item_id]
            ack: dict[str, Any] = {
                "id": item_id,
                "status": "active",
                "title": entry["title"],
                "priority": entry["priority"],
                "claim": {
                    "generation": took["claim"]["generation"],
                    "expires_at": took["claim"]["expires_at"],
                },
                "displaced": took["displaced"],
            }
            return self._claim_ack(snap, item_id, ack)

        return self._mutate(
            "takeover", f"{worker} takes over {item_id} from {expected_holder}: {reason}", apply,
        )

    def release(self, item_id: str, generation: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            out = S.op_release(snap, item_id, worker, generation)
            self._record_session(snap, item_id, "release", op_id, generation=generation)
            return out

        return self._mutate("release", f"{worker} releases {item_id}", apply)

    def prepare(
        self,
        item_id: str,
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
        implementation_dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        # Validate the external checkout before entering the Git publication
        # loop.  The pure operation repeats all shape/authority checks on any
        # retry, while this path rejects a caller-supplied stale or dirty path.
        try:
            clean_worktree = self._validate_source_checkout(source_worktree, source_revision)
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

        worker = self.worker

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            # Re-check on every publication retry: a concurrent state push
            # must not turn a formerly clean checkout into a stale receipt.
            batch_record = snap.index.get("batches", {}).get(batch_id)
            expected_repository = batch_record.get("repository") if isinstance(batch_record, dict) else None
            current_worktree = self._validate_source_checkout(
                clean_worktree, source_revision, expected_repository=expected_repository,
            )
            if isinstance(batch_record, dict) and S.batch_schema_of(batch_record) == S.BATCH_SCHEMA:
                integration_worktree = self._validate_source_checkout(
                    str(batch_record["integration_worktree"]), integration_head,
                    expected_branch=str(batch_record["integration_branch"]),
                    expected_repository=expected_repository,
                )
                registered_head = batch_record["integration_head"]
                if source_base not in {row["head"] for row in batch_record["integration_log"]}:
                    raise TodoError(
                        "source base is neither the batch start head nor a recorded integration head",
                        code=E_BATCH_HEAD,
                        recovery=["merge the current integration head into the member branch and prepare from it"],
                    )
                if not self._is_ancestor(integration_worktree, accepted_head, integration_head):
                    raise TodoError(
                        "integration head is missing the current accepted member", code=E_BATCH_HEAD,
                        recovery=["merge the accepted member head into the integration branch and prepare again"],
                    )
                if not self._is_ancestor(integration_worktree, registered_head, integration_head):
                    raise TodoError(
                        "prepared integration head does not descend from the registered integration head",
                        code=E_BATCH_HEAD,
                        recovery=["rebase the prepared head on the registered integration_head from show_batch"],
                    )
                for integrated_member, integrated_revision in batch_record.get("accepted_members", {}).items():
                    if integrated_member == member_id:
                        continue
                    if not self._is_ancestor(integration_worktree, integrated_revision, integration_head):
                        raise TodoError(
                            f"integration head is missing accepted member {integrated_member!r}",
                            code=E_BATCH_HEAD,
                            recovery=["merge every accepted member into the integration head and prepare again"],
                        )
            if not self._is_ancestor(current_worktree, source_base, accepted_head):
                raise TodoError(
                    "accepted member head is not based on its registered source base", code=E_STATE,
                    recovery=["prepare from the member branch based on source_base"],
                )
            changed_files = self._changed_paths(current_worktree, source_base, accepted_head)
            member_scope = batch_record["scope"][member_id] if isinstance(batch_record, dict) else []
            if any(not any(fnmatch.fnmatch(path, pattern) for pattern in member_scope) for path in changed_files):
                raise TodoError(
                    "prepared member range changes a path outside its frozen scope", code=E_STATE,
                    recovery=["move out-of-scope changes out of the member range"],
                )
            if isinstance(batch_record, dict) and S.batch_schema_of(batch_record) == S.BATCH_SCHEMA:
                unrecorded = set(self._changed_paths(integration_worktree, registered_head, integration_head))
                if not unrecorded <= set(changed_files):
                    raise TodoError(
                        "commits between the registered integration head and the prepared head touch paths "
                        f"outside this member: {sorted(unrecorded - set(changed_files))[:5]}",
                        code=E_BATCH_HEAD,
                        recovery=["record leader commits with advance_batch_head before landing the member"],
                    )
            detail = snap.details.get(item_id, {})
            dependencies = (detail.get("batch") or {}).get("implementation_dependencies", [])
            for dependency in dependencies:
                predecessor = snap.details.get(dependency, {}).get("prepared")
                if not isinstance(predecessor, dict):
                    raise TodoError(f"prepared dependency {dependency!r} is missing its accepted receipt", code=E_STATE)
                if not self._is_ancestor(current_worktree, predecessor["accepted_head"], accepted_head):
                    raise TodoError(f"prepared dependency {dependency!r} is absent from this member base", code=E_STATE)
            out = S.op_prepare(
                snap, item_id, worker, generation,
                batch_id=batch_id,
                member_id=member_id,
                source_worktree=current_worktree,
                source_revision=source_revision,
                source_base=source_base,
                accepted_head=accepted_head,
                integration_head=integration_head,
                scope_hash=scope_hash,
                changed_files=changed_files,
                verification=verification,
                implementation_dependencies=implementation_dependencies or [],
                op_id=op_id,
            )
            self._record_session(snap, item_id, "prepare", op_id, generation=generation)
            return out

        return self._mutate("prepare", f"{worker} prepares {item_id}", apply)

    def finish(self, item_id: str, generation: str, final_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            # A hold set after the claim was taken still guards completion.
            held = S.hold_until(item_id, snap, datetime.now(timezone.utc))
            if held is not None:
                raise TodoError(
                    f"task {item_id!r} is held until {held}; finish after then, "
                    "or clear it with update_item not_before=\"\"",
                    code=E_NOTHING_READY,
                )
            batch_meta = snap.details.get(item_id, {}).get("batch")
            if isinstance(batch_meta, dict):
                record = snap.index.get("batches", {}).get(batch_meta.get("batch_id"))
                if (
                    isinstance(record, dict)
                    and record.get("lifecycle", "active") == "active"
                    and S.batch_schema_of(record) != S.BATCH_SCHEMA
                ):
                    raise TodoError(
                        f"batch {batch_meta['batch_id']!r} predates batch v2; finish it with the client that registered it or abort it",
                        code=E_SCHEMA,
                        recovery=["finish or abort it with the client that registered it"],
                    )
            checked = final_evidence
            if checked is not None:
                checked = self._validate_final_checkout(snap, item_id, checked)
            out = S.op_finish(snap, item_id, worker, generation, checked)
            self._record_session(snap, item_id, "finish", op_id, generation=generation)
            return out

        return self._mutate("finish", f"{worker} finishes {item_id}", apply)

    def renew(self, item_id: str, generation: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            renewed = S.op_renew(snap, item_id, worker, generation, ttl_hours=self.ttl_hours)
            self._record_session(snap, item_id, "renew", op_id, generation=generation)
            return {"id": item_id, "expires_at": renewed["claim"]["expires_at"]}

        return self._mutate("renew", f"{worker} renews {item_id}", apply)

    def drop(self, item_id: str, reason: str, generation: str | None = None) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            out = S.op_drop(snap, item_id, worker, generation, reason=reason)
            self._record_session(snap, item_id, "drop", op_id, generation=generation)
            return out

        return self._mutate("drop", f"{worker} drops {item_id}", apply)

    def defer_item(self, item_id: str, summary: str, reason: str) -> dict[str, Any]:
        """Record deferred work on a task without changing its lifecycle."""

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            row = S.op_defer(snap, item_id, summary=summary, reason=reason)
            self._record_session(snap, item_id, "defer", op_id)
            return {"id": item_id, "deferral": {"id": row["id"], "resolution": "open"}}

        return self._mutate("defer", f"{self.worker} defers work on {item_id}", apply)

    def promote_deferral(
        self,
        item_id: str,
        deferral_id: int,
        *,
        to_item: str | None = None,
        title: str | None = None,
        priority: str = "medium",
        description: str = "",
        needs: list[str] | None = None,
        acceptance: list[str] | None = None,
        links: list[str] | None = None,
        context: str = "",
        batch: dict[str, Any] | None = None,
        not_before: str = "",
    ) -> dict[str, Any]:
        """Resolve an open deferral into a linked planning item, atomically."""
        requested = datetime.now(timezone.utc)
        seen: dict[str, Any] = {}

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            if seen:
                # Retry after a competing push: the row must still be the
                # open row we resolved, or this retry would re-promote a
                # resolution that already landed.
                current = S.query_deferrals(snap, from_item=item_id, resolution="all")
                match = next((row for row in current if row["id"] == deferral_id), None)
                if match is None or match.get("resolution", "open") != "open":
                    raise TodoError(
                        f"deferral {deferral_id} for {item_id!r} changed concurrently; "
                        "re-read and retry deliberately",
                        code=E_CONFLICT,
                    )
            out = S.op_promote_deferral(
                snap, item_id, deferral_id,
                to_item=to_item, title=title, priority=priority,
                description=description, needs=needs or [],
                acceptance=acceptance or [], links=links or [],
                context=context, batch=batch, not_before=not_before,
                now=requested,
            )
            seen.update(out)
            self._record_session(snap, item_id, "promote", op_id)
            if to_item is None:
                # This promotion created the successor: annotate its
                # history under the same operation ID so one commit links
                # both sides. Linking to a pre-existing item annotates
                # only the source task.
                try:
                    self._record_session(snap, out["resolved_item"], "promote", op_id)
                except TodoError:
                    pass
            return out

        return self._mutate("promote", f"{self.worker} promotes deferral {deferral_id} on {item_id}", apply)

    def dismiss_deferral(self, item_id: str, deferral_id: int, reason: str) -> dict[str, Any]:
        """Resolve an open deferral as deliberately dropped, with a reason."""

        def apply(snap: S.Snapshot, op_id: str) -> dict[str, Any]:
            out = S.op_dismiss_deferral(snap, item_id, deferral_id, reason=reason)
            self._record_session(snap, item_id, "dismiss", op_id)
            return out

        return self._mutate("dismiss", f"{self.worker} dismisses deferral {deferral_id} on {item_id}", apply)
