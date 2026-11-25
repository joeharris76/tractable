"""Batch v2 contract tests (ADR 0012): ownership, bases, overlap, advances, rebinding, recovery, cutover."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tractable import git_backend, store
from tractable.errors import TodoError
from tractable.service import TrackerService

GEN = "1" * 32
BATCH = "b1"


def h(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


def _verification(revision: str) -> dict[str, Any]:
    return {
        "status": "passed", "revision": revision, "clean": True,
        "suite": "bounded", "command": ["pytest", "-q"], "review": {"outcome": "clear"},
    }


def _meta(member: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {"batch_id": BATCH, "member_id": member, "implementation_dependencies": list(deps or [])}


# ---------------------------------------------------------------- pure store


def _pure(
    members: list[str],
    *,
    scope: dict[str, list[str]] | None = None,
    deps: dict[str, list[str]] | None = None,
    develop_ref: str | None = None,
    owner: str = "leader",
    metadata: bool = True,
) -> store.Snapshot:
    snap = store.Snapshot(index=store.empty_index(), details={})
    scope = scope or {member: [f"{member}/**", "shared/**"] for member in members}
    store.op_register_batch(
        snap, batch_id=BATCH, project_id="tractable", repository="repo", owner=owner,
        owner_generation=GEN, integration_branch="main", integration_worktree="/tmp/integration",
        start_head=h("start"), members=members, scope=scope, scope_hash=store.scope_digest(scope),
        delivery_boundary="final-pr", terminal_outcome="merged", develop_ref=develop_ref,
        confirm_schema3_cutover=True,
    )
    deps = deps or {}
    for member in members:
        store.op_create(
            snap, item_id=member, title=member.upper(), needs=deps.get(member, []),
            batch=_meta(member, deps.get(member)) if metadata else None,
        )
    return snap


def _head(snap: store.Snapshot) -> str:
    return snap.index["batches"][BATCH]["integration_head"]


def _prep(
    snap: store.Snapshot,
    member: str,
    *,
    base: str | None = None,
    head: str | None = None,
    files: list[str] | None = None,
    deps: list[str] | None = None,
    worker: str = "leader",
    accepted: str | None = None,
) -> dict[str, Any]:
    base = base or h("start")
    accepted = accepted or h(f"accepted-{member}-{head}")
    head = head or h(f"landed-{member}-{len(snap.index['batches'][BATCH]['integration_log'])}")
    claim = snap.index["items"][member].get("claim")
    generation = claim["generation"] if claim else store.op_take(snap, member, worker)["claim"]["generation"]
    return store.op_prepare(
        snap, member, worker, generation, batch_id=BATCH, member_id=member,
        source_worktree="/tmp/src", source_revision=accepted, source_base=base, accepted_head=accepted,
        integration_head=head, scope_hash=snap.index["batches"][BATCH]["scope_hash"],
        verification=_verification(accepted), changed_files=files or [], implementation_dependencies=deps or [],
    )


def _bind(snap: store.Snapshot, head: str | None = None, *, number: int = 1, node: str = "PR_1", repo: str = "o/r", **kw: Any):
    return store.op_bind_batch_pr(
        snap, BATCH, "leader", GEN, pr_repository=repo, number=number, node_id=node,
        head=head or _head(snap), **kw,
    )


def _final(snap: store.Snapshot, members: list[str], acceptance: dict[str, Any] | None = None) -> dict[str, Any]:
    head = _head(snap)
    receipts = {m: snap.details[m]["prepared"] for m in members}
    evidence = {
        "status": "passed", "project_id": "tractable", "repository": "repo", "batch_id": BATCH,
        "tree_worktree": "/tmp/integration", "tree_revision": head, "integration_branch": "main",
        "integration_head": head, "scope_hash": snap.index["batches"][BATCH]["scope_hash"],
        "member_heads": {m: r["accepted_head"] for m, r in receipts.items()},
        "member_ranges": {m: {"base": r["source_base"], "head": r["accepted_head"]} for m, r in receipts.items()},
        "changed_files": [], "final_pr": snap.index["batches"][BATCH]["final_pr"],
        "suite": "combined-tree", "clean": True,
    }
    if acceptance is not None:
        evidence["current_acceptance"] = acceptance
    return evidence


def _accept(snap: store.Snapshot, members: list[str]) -> dict[str, Any]:
    return {
        m: {
            "status": "passed", "revision": _head(snap), "clean": True, "worktree": "/tmp/integration",
            "suite": "bounded", "command": ["pytest", "-q"],
        }
        for m in members
    }


def _finish(snap: store.Snapshot, member: str, evidence: dict[str, Any]) -> dict[str, Any]:
    generation = store.op_take(snap, member, "leader")["claim"]["generation"]
    return store.op_finish(snap, member, "leader", generation, evidence)


# ------------------------------------------------------------- B1: ownership


def test_owner_take_succeeds_after_foreign_refusal() -> None:
    snap = _pure(["a"])
    with pytest.raises(TodoError) as exc:
        store.op_take(snap, "a", "outsider")
    assert exc.value.code == "E_BATCH_OWNER"
    assert "'leader'" in str(exc.value)
    assert exc.value.recovery
    assert snap.index["items"]["a"]["claim"] is None
    assert store.op_take(snap, "a", "leader")["claim"]["worker"] == "leader"


def test_registered_member_without_metadata_is_reserved() -> None:
    snap = _pure(["a"], metadata=False)
    assert "batch" not in snap.details["a"]
    with pytest.raises(TodoError) as exc:
        store.op_take(snap, "a", "outsider")
    assert exc.value.code == "E_BATCH_OWNER"


def test_takeover_of_active_member_by_non_owner_is_refused_and_owner_may_takeover() -> None:
    snap = _pure(["a"])
    snap.index["items"]["a"]["claim"] = {
        "worker": "stray", "expires_at": "2099-01-01T00:00:00Z", "generation": "2" * 32, "renewals": 0,
    }
    snap.index["items"]["a"]["status"] = "active"
    with pytest.raises(TodoError) as exc:
        store.op_takeover(snap, "a", "outsider", "stray", "stray session died")
    assert exc.value.code == "E_BATCH_OWNER"
    took = store.op_takeover(snap, "a", "leader", "stray", "stray session died")
    assert took["claim"]["worker"] == "leader" and "transferred_batches" not in took
    assert snap.index["batches"][BATCH]["owner"] == "leader"


def test_members_of_aborted_batch_follow_ordinary_claims() -> None:
    snap = _pure(["a"])
    store.op_abort_batch(snap, BATCH, "leader", GEN)
    assert store.op_take(snap, "a", "outsider")["claim"]["worker"] == "outsider"


def test_members_of_completed_batch_follow_ordinary_claims() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    _bind(snap)
    _finish(snap, "a", _final(snap, ["a"]))
    assert snap.index["batches"][BATCH]["lifecycle"] == "completed"
    store.op_create(snap, item_id="free", title="Free")
    assert store.op_take(snap, "free", "outsider")["claim"]["worker"] == "outsider"


def test_previous_owner_cannot_prepare_or_finish_after_recovery() -> None:
    snap = _pure(["a"])
    claim = store.op_take(snap, "a", "leader")
    store.op_recover_batch_owner(
        snap, BATCH, "rescuer", expected_owner="leader", expected_owner_generation=GEN,
        new_owner_generation="3" * 32, reason="leader died",
    )
    with pytest.raises(TodoError) as exc:
        store.op_prepare(
            snap, "a", "leader", claim["claim"]["generation"], batch_id=BATCH, member_id="a",
            source_worktree="/tmp/src", source_revision=h("x"), source_base=h("start"), accepted_head=h("x"),
            integration_head=h("y"), scope_hash=snap.index["batches"][BATCH]["scope_hash"],
            verification=_verification(h("x")),
        )
    assert exc.value.code == "E_BATCH_OWNER"
    with pytest.raises(TodoError) as exc:
        store.op_finish(snap, "a", "leader", claim["claim"]["generation"])
    assert exc.value.code == "E_BATCH_OWNER"
    store.op_release(snap, "a", "leader", claim["claim"]["generation"])


def test_register_refuses_members_claimed_by_another_worker() -> None:
    snap = store.Snapshot(index=store.empty_index(), details={})
    store.op_create(snap, item_id="a", title="A")
    store.op_take(snap, "a", "stray")
    scope = {"a": ["a/**"]}
    with pytest.raises(TodoError) as exc:
        store.op_register_batch(
            snap, batch_id=BATCH, project_id="p", repository="repo", owner="leader", owner_generation=GEN,
            integration_branch="main", integration_worktree="/tmp/integration", start_head=h("start"),
            members=["a"], scope=scope, scope_hash=store.scope_digest(scope), delivery_boundary="final-pr",
            terminal_outcome="merged", confirm_schema3_cutover=True,
        )
    assert exc.value.code == "E_CONFLICT"


# ------------------------------------------------------- B2: dispatch bases


def test_synced_member_prepares_from_recorded_integration_head() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    recorded = _head(snap)
    out = _prep(snap, "b", base=recorded, files=["b/f"])
    assert out["status"] == "open"
    assert snap.details["b"]["prepared"]["source_base"] == recorded
    assert snap.details["b"]["prepared"]["schema"] == "prepared_work_v2"
    log = snap.index["batches"][BATCH]["integration_log"]
    assert [row["kind"] for row in log] == ["start", "prepare", "prepare"]


def test_unrecorded_base_refused() -> None:
    snap = _pure(["a"])
    with pytest.raises(TodoError) as exc:
        _prep(snap, "a", base=h("never-recorded"))
    assert exc.value.code == "E_BATCH_HEAD"
    assert "receipt" not in snap.details["a"] and "prepared" not in snap.details["a"]


def test_prepare_integration_head_must_be_new() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a")
    with pytest.raises(TodoError) as exc:
        _prep(snap, "b", head=_head(snap))
    assert exc.value.code == "E_BATCH_HEAD"


def test_receipt_keeps_the_head_it_was_integrated_at() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    first = snap.index["batches"][BATCH]["integrated_members"]["a"]
    _prep(snap, "b", base=_head(snap), files=["b/f"])
    assert snap.index["batches"][BATCH]["integrated_members"]["a"] == first
    assert snap.details["a"]["prepared"]["integration_head"] == first["integration_head"]
    assert snap.index["batches"][BATCH]["integration_head"] != first["integration_head"]


def test_dependent_member_needs_no_current_head_base() -> None:
    snap = _pure(["a", "b"], deps={"b": ["a"]})
    _prep(snap, "a", files=["a/f"])
    assert store.is_ready("b", snap)
    _prep(snap, "b", base=h("start"), files=["b/f"], deps=["a"])
    assert snap.details["b"]["prepared"]["source_base"] == h("start")


# --------------------------------------------------------------- B4: rebind


def test_rebind_moves_head_with_expected_previous_head() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    first = _head(snap)
    _bind(snap)
    store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=first, head=h("fix-1"), reason="fix", changed_files=["a/f"],
    )
    with pytest.raises(TodoError) as exc:
        _bind(snap)
    assert exc.value.code == "E_CONFLICT"
    with pytest.raises(TodoError) as exc:
        _bind(snap, h("fix-1"), expected_previous_head=h("wrong"))
    assert exc.value.code == "E_CONFLICT"
    out = _bind(snap, h("fix-1"), expected_previous_head=first)
    assert out["idempotent"] is False
    pr = snap.index["batches"][BATCH]["final_pr"]
    assert pr == {"repository": "o/r", "number": 1, "node_id": "PR_1", "head": h("fix-1")}
    history = snap.index["batches"][BATCH]["final_pr_history"]
    assert [row["previous_head"] for row in history] == [None, first]


def test_rebind_different_pr_refused() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    _bind(snap)
    for kwargs in ({"number": 2}, {"node": "PR_other"}, {"repo": "o/other"}):
        with pytest.raises(TodoError) as exc:
            _bind(snap, **kwargs)
        assert exc.value.code == "E_CONFLICT"
    assert snap.index["batches"][BATCH]["final_pr"]["number"] == 1


def test_rebind_retry_idempotent() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    first = _head(snap)
    assert _bind(snap)["idempotent"] is False
    again = _bind(snap)
    assert again["idempotent"] is True and again["_no_write"] is True
    store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=first, head=h("fix-1"), reason="fix", changed_files=["a/f"],
    )
    _bind(snap, h("fix-1"), expected_previous_head=first)
    before = json.dumps(snap.index, sort_keys=True)
    retry = _bind(snap, h("fix-1"), expected_previous_head=first)
    assert retry["idempotent"] is True
    assert json.dumps(snap.index, sort_keys=True) == before
    with pytest.raises(TodoError) as exc:
        _bind(snap, h("fix-1"), expected_previous_head=h("other"))
    assert exc.value.code == "E_CONFLICT"


def test_rebind_guard_is_required_and_nothing_to_revise_is_refused() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    first = _head(snap)
    with pytest.raises(TodoError) as exc:
        _bind(snap, expected_previous_head=first)
    assert exc.value.code == "E_CONFLICT"
    _bind(snap)
    store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=first, head=h("fix-1"), reason="fix", changed_files=["a/f"],
    )
    with pytest.raises(TodoError) as exc:
        _bind(snap, h("fix-1"))
    assert exc.value.code == "E_CONFLICT"


def test_evidence_for_previous_pr_head_refused() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    first = _head(snap)
    _bind(snap)
    stale = _final(snap, ["a"])
    store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=first, head=h("fix-1"), reason="fix", changed_files=["a/f"],
    )
    _bind(snap, h("fix-1"), expected_previous_head=first)
    with pytest.raises(TodoError) as exc:
        _finish(snap, "a", stale)
    assert exc.value.code == "E_FINAL_EVIDENCE"
    _finish(snap, "a", _final(snap, ["a"], _accept(snap, ["a"])))
    assert snap.index["items"]["a"]["status"] == "done"


def test_head_changes_are_refused_once_a_member_is_done() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a")
    _prep(snap, "b", base=_head(snap))
    _bind(snap)
    _finish(snap, "a", _final(snap, ["a", "b"]))
    with pytest.raises(TodoError) as exc:
        store.op_advance_batch_head(
            snap, BATCH, "leader", GEN, expected_head=_head(snap), head=h("late"), reason="fix",
            changed_files=["a/f"],
        )
    assert exc.value.code == "E_STATE"


# ------------------------------------------------------ advance (pure rules)


def test_advance_stale_expected_head_refused() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    with pytest.raises(TodoError) as exc:
        store.op_advance_batch_head(
            snap, BATCH, "leader", GEN, expected_head=h("start"), head=h("fix-1"), reason="fix",
            changed_files=["a/f"],
        )
    assert exc.value.code == "E_CONFLICT"
    assert len(snap.index["batches"][BATCH]["integration_log"]) == 2


def test_advance_non_owner_refused() -> None:
    snap = _pure(["a"])
    for worker, generation in (("outsider", GEN), ("leader", "9" * 32)):
        with pytest.raises(TodoError) as exc:
            store.op_advance_batch_head(
                snap, BATCH, worker, generation, expected_head=h("start"), head=h("fix-1"), reason="fix",
                changed_files=["a/f"],
            )
        assert exc.value.code == "E_CLAIM_STALE"


def test_advance_exact_retry_idempotent_even_after_a_later_reprepare() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    first = _head(snap)
    out = store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=first, head=h("fix-1"), reason="fix", changed_files=["a/f"],
    )
    assert out["idempotent"] is False and out["requires_acceptance"] == ["a"]
    store.op_update(snap, "a", description="changed")
    _prep(snap, "a", base=h("start"), head=_head(snap), files=["a/f"])
    assert snap.index["batches"][BATCH]["integration_log"][-1]["kind"] == "reprepare"
    retry = store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=first, head=h("fix-1"), reason="fix", changed_files=["a/f"],
    )
    assert retry["idempotent"] is True and retry["seq"] == out["seq"]


def test_fix_outside_member_scope_is_refused() -> None:
    snap = _pure(["a"])
    with pytest.raises(TodoError) as exc:
        store.op_advance_batch_head(
            snap, BATCH, "leader", GEN, expected_head=h("start"), head=h("fix-1"), reason="fix",
            changed_files=["elsewhere/f"],
        )
    assert exc.value.code == "E_BATCH_HEAD"


def test_develop_merge_needs_a_registered_develop_ref() -> None:
    snap = _pure(["a"])
    with pytest.raises(TodoError) as exc:
        store.op_advance_batch_head(
            snap, BATCH, "leader", GEN, expected_head=h("start"), head=h("m"), reason="develop-merge",
            changed_files=["docs/x"], merged_parent=h("p"),
        )
    assert exc.value.code == "E_BATCH_HEAD"
    with pytest.raises(TodoError):
        _pure(["a"], develop_ref="origin/develop")


# --------------------------------------------------------- B6: overlap/accept


def test_same_file_evolution_records_overlap() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["shared/f"])
    out = _prep(snap, "b", base=_head(snap), files=["b/f", "shared/f"])
    assert out["prepared"]["overlaps"] == [{"kind": "member", "member": "a", "paths": ["shared/f"]}]
    required = store.acceptance_required(snap, BATCH)
    assert list(required) == ["a"] and required["a"][0]["by"] == "b"
    disjoint = _pure(["a", "b"])
    _prep(disjoint, "a", files=["a/f"])
    assert _prep(disjoint, "b", base=_head(disjoint), files=["b/f"])["prepared"]["overlaps"] == []
    assert store.acceptance_required(disjoint, BATCH) == {}


def test_silent_revert_blocked_without_acceptance_rerun() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["shared/f"])
    _prep(snap, "b", base=_head(snap), files=["shared/f"])
    _bind(snap)
    with pytest.raises(TodoError) as exc:
        _finish(snap, "b", _final(snap, ["a", "b"]))
    assert exc.value.code == "E_FINAL_EVIDENCE"
    assert "'a'" in str(exc.value)
    with pytest.raises(TodoError) as exc:
        _finish(snap, "b", _final(snap, ["a", "b"], {}))
    assert exc.value.code == "E_FINAL_EVIDENCE"
    stale = _accept(snap, ["a"])
    stale["a"]["revision"] = h("earlier")
    with pytest.raises(TodoError):
        _finish(snap, "b", _final(snap, ["a", "b"], stale))
    weaker = _accept(snap, ["a"])
    weaker["a"]["command"] = ["true"]
    with pytest.raises(TodoError):
        _finish(snap, "b", _final(snap, ["a", "b"], weaker))
    snap.index["items"]["b"]["claim"] = None
    snap.index["items"]["b"]["status"] = "open"
    _finish(snap, "b", _final(snap, ["a", "b"], _accept(snap, ["a"])))
    assert snap.index["items"]["b"]["status"] == "done"


def test_fix_touching_earlier_member_requires_acceptance() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    _prep(snap, "b", base=_head(snap), files=["b/f"])
    out = store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=_head(snap), head=h("fix-1"), reason="fix",
        changed_files=["a/f", "shared/g"],
    )
    assert out["requires_acceptance"] == ["a"]
    assert list(store.acceptance_required(snap, BATCH)) == ["a"]
    _bind(snap)
    with pytest.raises(TodoError) as exc:
        _finish(snap, "a", _final(snap, ["a", "b"]))
    assert exc.value.code == "E_FINAL_EVIDENCE"
    snap.index["items"]["a"]["claim"] = None
    snap.index["items"]["a"]["status"] = "open"
    _finish(snap, "a", _final(snap, ["a", "b"], _accept(snap, ["a"])))


def test_fix_overlap_recorded_for_member_dispatched_before_the_fix() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=_head(snap), head=h("fix-1"), reason="fix",
        changed_files=["shared/g"],
    )
    out = _prep(snap, "b", base=h("start"), files=["shared/g"])
    assert out["prepared"]["overlaps"] == [{"kind": "fix", "seq": 2, "paths": ["shared/g"]}]
    assert list(store.acceptance_required(snap, BATCH)) == ["b"]
    fresh = _pure(["a", "b"])
    _prep(fresh, "a", files=["a/f"])
    store.op_advance_batch_head(
        fresh, BATCH, "leader", GEN, expected_head=_head(fresh), head=h("fix-1"), reason="fix",
        changed_files=["shared/g"],
    )
    synced = _prep(fresh, "b", base=h("fix-1"), files=["shared/g"])
    assert synced["prepared"]["overlaps"] == []


def test_develop_merge_excluded_from_union() -> None:
    snap = _pure(["a"], develop_ref="refs/remotes/origin/develop")
    _prep(snap, "a", files=["a/f"])
    out = store.op_advance_batch_head(
        snap, BATCH, "leader", GEN, expected_head=_head(snap), head=h("merge"), reason="develop-merge",
        changed_files=["docs/x", "a/f"], merged_parent=h("develop-tip"),
    )
    assert out["requires_acceptance"] == []
    assert store.excluded_paths(snap, BATCH) == {"docs/x"}
    _bind(snap)
    evidence = _final(snap, ["a"])
    evidence["changed_files"] = ["a/f"]
    _finish(snap, "a", evidence)
    other = _pure(["a"], develop_ref="refs/remotes/origin/develop")
    _prep(other, "a", files=["a/f"])
    store.op_advance_batch_head(
        other, BATCH, "leader", GEN, expected_head=_head(other), head=h("fix"), reason="fix",
        changed_files=["a/f"],
    )
    assert store.excluded_paths(other, BATCH) == set()
    bad = _pure(["a"])
    _prep(bad, "a", files=["a/f"])
    _bind(bad)
    evidence = _final(bad, ["a"])
    evidence["changed_files"] = ["docs/x"]
    with pytest.raises(TodoError) as exc:
        _finish(bad, "a", evidence)
    assert exc.value.code == "E_FINAL_EVIDENCE"


# ----------------------------------------------------- re-prepare and closeout


def test_reprepare_after_invalidating_edit() -> None:
    snap = _pure(["a"])
    _prep(snap, "a", files=["a/f"])
    landed = _head(snap)
    store.op_update(snap, "a", description="clarified")
    assert "prepared" not in snap.details["a"]
    accepted = snap.index["batches"][BATCH]["accepted_members"]["a"]
    out = _prep(snap, "a", base=h("start"), head=landed, files=["a/f"], accepted=accepted)
    assert out["prepared"]["integration_head"] == landed
    log = snap.index["batches"][BATCH]["integration_log"]
    assert log[-1]["kind"] == "reprepare" and log[-1]["head"] == log[-2]["head"]
    assert _head(snap) == landed
    store.validate_snapshot(snap.index, snap.details)


def test_reprepare_with_new_code_replaces_entries() -> None:
    snap = _pure(["a"])
    _prep(snap, "a", files=["a/f"])
    old = snap.index["batches"][BATCH]["integrated_members"]["a"]
    store.op_update(snap, "a", acceptance=["new criterion"])
    _prep(snap, "a", base=_head(snap), head=h("landed-again"), files=["a/f2"], accepted=h("fresh-code"))
    batch = snap.index["batches"][BATCH]
    assert batch["integrated_members"]["a"]["accepted_head"] == h("fresh-code") != old["accepted_head"]
    assert batch["accepted_members"]["a"] == h("fresh-code")
    assert batch["integration_log"][-1]["kind"] == "prepare"
    assert snap.details["a"]["prepared"]["changed_files"] == ["a/f2"]
    store.validate_snapshot(snap.index, snap.details)


def test_invalidating_edit_refused_after_member_done() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a")
    _prep(snap, "b", base=_head(snap))
    _bind(snap)
    _finish(snap, "a", _final(snap, ["a", "b"]))
    with pytest.raises(TodoError) as exc:
        store.op_update(snap, "b", description="late edit")
    assert exc.value.code == "E_STATE"
    assert "prepared" in snap.details["b"]
    store.op_update(snap, "b", context_append="note during closeout")
    assert "prepared" in snap.details["b"]


# ---------------------------------------------------------- B5: metadata edits


def _chain() -> store.Snapshot:
    snap = _pure(["a", "b", "c"], deps={"b": ["a"], "c": ["b"]})
    _prep(snap, "a", files=["a/f"])
    _prep(snap, "b", files=["b/f"], deps=["a"])
    _prep(snap, "c", files=["c/f"], deps=["b"])
    return snap


def _receipts(snap: store.Snapshot) -> list[bool]:
    return ["prepared" in snap.details[m] for m in ("a", "b", "c")]


def test_context_edit_keeps_receipt() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    out = store.op_update(snap, "a", context="new notes")
    assert "prepared" in snap.details["a"] and out["changed"] == ["context"]
    assert snap.details["a"]["context"] == "new notes"


def test_context_append_keeps_receipt_and_dependents() -> None:
    snap = _chain()
    store.op_update(snap, "a", context_append="first")
    out = store.op_update(snap, "a", context_append="second")
    assert _receipts(snap) == [True, True, True]
    assert out["invalidated"] == []
    assert snap.details["a"]["context"] == "first\n\nsecond"
    assert store.is_ready("b", snap) is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "different"),
        ("acceptance", ["new criterion"]),
        ("links", ["https://example.invalid/x"]),
        ("title", "Renamed"),
        ("priority", "high"),
    ],
)
def test_content_edits_invalidate_receipt_and_dependents(field: str, value: Any) -> None:
    snap = _chain()
    out = store.op_update(snap, "a", **{field: value})
    assert _receipts(snap) == [False, False, False]
    assert out["invalidated"] == ["b", "c"]


def test_needs_edit_invalidates_receipt_and_dependents() -> None:
    snap = _pure(["a", "b", "c"], deps={"c": ["b"]})
    store.op_create(snap, item_id="extra", title="Extra")
    _prep(snap, "a")
    _prep(snap, "b")
    _prep(snap, "c", deps=["b"])
    out = store.op_update(snap, "b", needs=["extra"])
    assert out["invalidated"] == ["c"]
    assert "prepared" not in snap.details["b"] and "prepared" not in snap.details["c"]
    assert "prepared" in snap.details["a"]


def test_description_edit_invalidates_receipt_and_dependents() -> None:
    snap = _chain()
    assert store.op_update(snap, "b", description="changed")["invalidated"] == ["c"]
    assert _receipts(snap) == [True, False, False]


def test_acceptance_edit_invalidates_receipt_and_dependents() -> None:
    snap = _chain()
    assert store.op_update(snap, "b", acceptance=["x"])["invalidated"] == ["c"]
    assert _receipts(snap) == [True, False, False]


def test_links_edit_invalidates_receipt_and_dependents() -> None:
    snap = _chain()
    assert store.op_update(snap, "b", links=["https://example.invalid/y"])["invalidated"] == ["c"]
    assert _receipts(snap) == [True, False, False]


def test_batch_metadata_edit_refused_receipt_kept() -> None:
    snap = _chain()
    with pytest.raises(TodoError) as exc:
        store.op_update(snap, "a", batch=_meta("a", ["b"]))
    assert exc.value.code == "E_STATE"
    assert _receipts(snap) == [True, True, True]


def test_scope_has_no_edit_path_receipt_kept() -> None:
    snap = _chain()
    before = json.dumps(snap.index["batches"][BATCH]["scope"], sort_keys=True)
    with pytest.raises(TypeError):
        store.op_update(snap, "a", scope={"a": ["**"]})  # type: ignore[call-arg]
    with pytest.raises(TodoError):
        store.op_update(snap, "a", batch={**_meta("a"), "scope": ["**"]})
    assert json.dumps(snap.index["batches"][BATCH]["scope"], sort_keys=True) == before
    assert _receipts(snap) == [True, True, True]


def test_mixed_context_and_acceptance_edit_invalidates() -> None:
    snap = _chain()
    store.op_update(snap, "a", context="notes", acceptance=["changed"])
    assert _receipts(snap) == [False, False, False]


# -------------------------------------------------- schema, cutover, receipts


def _plain_snap(version: int) -> store.Snapshot:
    snap = store.Snapshot(index=store.empty_index(), details={})
    snap.index["schema_version"] = version
    store.op_create(snap, item_id="a", title="A")
    return snap


def _register_plain(snap: store.Snapshot, **kwargs: Any) -> dict[str, Any]:
    scope = {"a": ["a/**"]}
    params: dict[str, Any] = dict(
        batch_id=BATCH, project_id="p", repository="repo", owner="leader", owner_generation=GEN,
        integration_branch="main", integration_worktree="/tmp/integration", start_head=h("start"),
        members=["a"], scope=scope, scope_hash=store.scope_digest(scope), delivery_boundary="final-pr",
        terminal_outcome="merged",
    )
    params.update(kwargs)
    return store.op_register_batch(snap, **params)


def test_cutover_from_schema_1_one_commit_and_refused_without_flag(tmp_path: Path) -> None:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "--quiet", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@e.invalid"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "init"], check=True)
    start = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def seed(snap: store.Snapshot) -> dict[str, Any]:
        store.op_create(snap, item_id="a", title="A")
        snap.index["schema_version"] = 1
        return {"id": "seed"}

    assert git_backend.mutate(ref, op="update", summary="seed schema 1", worker="seeder", apply=seed).ok
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="leader", session_id="s-leader")
    scope = {"a": ["a/**"]}
    args = dict(
        batch_id=BATCH, project_id="p", repository=str(repo), owner_generation=GEN, integration_branch="main",
        integration_worktree=str(repo), start_head=start, members=["a"], scope=scope,
        scope_hash=store.scope_digest(scope), delivery_boundary="final-pr", terminal_outcome="merged",
    )
    refused = svc.register_batch(**args)
    assert not refused["ok"] and refused["code"] == "E_STATE"
    assert git_backend.read(ref, tmp_path / "cache").snapshot.index["schema_version"] == 1

    def count() -> int:
        return int(subprocess.check_output(
            ["git", "-C", str(remote), "rev-list", "--count", "todo-state"], text=True).strip())

    before = count()
    done = svc.register_batch(**args, confirm_schema3_cutover=True)
    assert done["ok"], done
    assert done["data"]["schema_version"] == 4 and done["data"]["migrated_from"] == 1
    assert count() == before + 1
    assert git_backend.read(ref, tmp_path / "cache").snapshot.index["schema_version"] == 4


def test_cutover_from_schema_3_refused_with_active_batch() -> None:
    snap = _plain_snap(4)
    _register_plain(snap)
    snap.index["schema_version"] = 3
    for record in snap.index["batches"].values():
        record.pop("batch_schema")
        for key in ("owner_epoch", "integration_log", "recoveries", "final_pr_history"):
            record.pop(key)
    store.op_create(snap, item_id="z", title="Z")
    scope = {"z": ["z/**"]}
    with pytest.raises(TodoError) as exc:
        _register_plain(
            snap, batch_id="b2", members=["z"], scope=scope, scope_hash=store.scope_digest(scope),
            confirm_schema3_cutover=True,
        )
    assert exc.value.code == "E_STATE" and exc.value.recovery
    assert snap.index["schema_version"] == 3
    snap.index["batches"][BATCH]["lifecycle"] = "aborted"
    _register_plain(
        snap, batch_id="b2", members=["z"], scope=scope, scope_hash=store.scope_digest(scope),
        confirm_schema3_cutover=True,
    )
    assert snap.index["schema_version"] == 4
    assert snap.index["batches"][BATCH]["batch_schema"] == 1
    assert snap.index["batches"]["b2"]["batch_schema"] == 2


def test_cutover_refused_with_foreign_live_claim() -> None:
    snap = _plain_snap(1)
    store.op_create(snap, item_id="other", title="Other")
    store.op_take(snap, "other", "someone-else")
    with pytest.raises(TodoError) as exc:
        _register_plain(snap, confirm_schema3_cutover=True)
    assert exc.value.code == "E_ACTIVE_CLAIMS"
    assert "other" in str(exc.value)
    assert snap.index["schema_version"] == 1 and "b1" not in snap.index.get("batches", {})
    store.op_release(snap, "other", "someone-else", snap.index["items"]["other"]["claim"]["generation"])
    store.op_take(snap, "other", "leader")
    assert _register_plain(snap, confirm_schema3_cutover=True)["migrated_from"] == 1


def test_cutover_flag_not_needed_once_schema_4() -> None:
    snap = _plain_snap(4)
    assert _register_plain(snap)["migrated_from"] is None


def test_v1_client_refuses_schema_4_with_e_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _plain_snap(1)
    _register_plain(snap, confirm_schema3_cutover=True)
    index, details = snap.index, snap.details
    assert index["schema_version"] == 4
    monkeypatch.setattr(store, "SUPPORTED_SCHEMA_VERSIONS", (1, 2, 3))
    with pytest.raises(TodoError) as exc:
        store.validate_snapshot(index, details)
    assert exc.value.code == "E_SCHEMA"


def test_batch_metadata_write_below_schema_4_refused() -> None:
    snap = _plain_snap(3)
    store.op_create(snap, item_id="b", title="B")
    with pytest.raises(TodoError) as exc:
        store.op_create(snap, item_id="c", title="C", batch=_meta("c"))
    assert exc.value.code == "E_SCHEMA"
    with pytest.raises(TodoError) as exc:
        store.op_update(snap, "b", batch=_meta("b"))
    assert exc.value.code == "E_SCHEMA"
    assert snap.index["schema_version"] == 3 and "batch" not in snap.details["b"]


def test_v1_receipt_not_reinterpreted_under_v2() -> None:
    snap = _pure(["a"])
    _prep(snap, "a")
    receipt = snap.details["a"]["prepared"]
    receipt["schema"] = "prepared_work_v1"
    receipt["owner_generation"] = GEN
    for key in ("owner_epoch", "integration_seq", "overlaps"):
        receipt.pop(key)
    with pytest.raises(TodoError) as exc:
        store.validate_snapshot(snap.index, snap.details)
    assert exc.value.code == "E_SCHEMA"


def test_new_stores_start_below_the_cutover_schema() -> None:
    assert store.empty_index()["schema_version"] == 3
    assert store.SCHEMA_VERSION == 4 and 4 in store.SUPPORTED_SCHEMA_VERSIONS


def test_batch_capability_names_the_v2_contract() -> None:
    capability = store.batch_capability("0.15.0")
    assert capability == {
        "version": 2, "receipt_schema": "prepared_work_v2", "store_schema_version": 4,
        "supported_schema_versions": [1, 2, 3, 4], "tool_version": "0.15.0",
        "tools": ["advance_batch_head", "recover_batch_owner", "show_batch"],
        "features": {
            "integration_worktree_relocation": True,
        },
    }

# --------------------------------------------------------- recovery (pure)


def _recover(snap: store.Snapshot, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = dict(
        expected_owner="leader", expected_owner_generation=GEN, new_owner_generation="3" * 32,
        reason="leader session died", session_id="s-rescuer", client="pytest", op_id="a" * 32,
    )
    params.update(overrides)
    return store.op_recover_batch_owner(snap, BATCH, "rescuer", **params)


def test_recover_owner_with_no_member_claim() -> None:
    snap = _pure(["a", "b"])
    out = _recover(snap)
    batch = snap.index["batches"][BATCH]
    assert out["owner"] == "rescuer" and batch["owner"] == "rescuer"
    assert batch["owner_generation"] == "3" * 32 and batch["owner_epoch"] == 1
    assert batch["recoveries"][0]["previous_owner"] == "leader"
    assert "owner_generation" not in json.dumps(batch["recoveries"])
    assert store.op_take(snap, "a", "rescuer")["claim"]["worker"] == "rescuer"


def test_recover_wrong_owner_or_generation_refused() -> None:
    snap = _pure(["a"])
    with pytest.raises(TodoError) as exc:
        _recover(snap, expected_owner="someone-else")
    assert exc.value.code == "E_CONFLICT"
    with pytest.raises(TodoError) as exc:
        _recover(snap, expected_owner_generation="9" * 32)
    assert exc.value.code == "E_CLAIM_STALE"
    assert GEN not in str(exc.value) and "9" * 32 not in str(exc.value)
    with pytest.raises(TodoError):
        _recover(snap, reason="  ")
    with pytest.raises(TodoError):
        _recover(snap, new_owner_generation="short")
    with pytest.raises(TodoError):
        _recover(snap, new_owner_generation=GEN)
    assert snap.index["batches"][BATCH]["owner"] == "leader"


def test_recover_retry_after_lost_reply_idempotent() -> None:
    snap = _pure(["a"])
    first = _recover(snap)
    assert first["idempotent"] is False
    before = json.dumps(snap.index, sort_keys=True)
    retry = _recover(snap)
    assert retry["idempotent"] is True and retry["owner_epoch"] == 1
    assert json.dumps(snap.index, sort_keys=True) == before
    with pytest.raises(TodoError):
        _recover(snap, new_owner_generation="4" * 32)


def test_recovered_previous_generation_is_stale() -> None:
    snap = _pure(["a"])
    _recover(snap)
    for call in (
        lambda: store.op_abort_batch(snap, BATCH, "leader", GEN),
        lambda: store.op_abort_batch(snap, BATCH, "rescuer", GEN),
        lambda: _bind(snap),
        lambda: store.op_advance_batch_head(
            snap, BATCH, "rescuer", GEN, expected_head=h("start"), head=h("f"), reason="fix", changed_files=["a/f"],
        ),
    ):
        with pytest.raises(TodoError) as exc:
            call()
        assert exc.value.code == "E_CLAIM_STALE"
    assert store.op_abort_batch(snap, BATCH, "rescuer", "3" * 32)["lifecycle"] == "aborted"


def test_receipts_survive_recovery() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    _recover(snap)
    store.validate_snapshot(snap.index, snap.details)
    assert snap.details["a"]["prepared"]["owner_epoch"] == 0
    _prep(snap, "b", base=_head(snap), files=["b/f"], worker="rescuer")
    assert snap.details["b"]["prepared"]["owner_epoch"] == 1
    store.op_bind_batch_pr(
        snap, BATCH, "rescuer", "3" * 32, pr_repository="o/r", number=1, node_id="PR_1", head=_head(snap),
    )
    generation = store.op_take(snap, "a", "rescuer")["claim"]["generation"]
    store.op_finish(snap, "a", "rescuer", generation, _final(snap, ["a", "b"]))


# ------------------------------------------------------------- real Git flow


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


class Flow:
    """A real integration checkout, member worktrees and a state branch."""

    def __init__(
        self, tmp_path: Path, members: list[str], scope: dict[str, list[str]], *,
        deps: dict[str, list[str]] | None = None, develop: bool = False,
    ) -> None:
        self.tmp = tmp_path
        self.members = members
        self.deps = deps or {}
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "--quiet", "-b", "main")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        self.write(self.repo, {"README": "start\n", "shared/f": "line\n", "a/f1": "one\n"})
        self.commit(self.repo, "start")
        self.start = self.rev()
        self.develop_tip = None
        if develop:
            _git(self.repo, "checkout", "--quiet", "-b", "develop-side")
            self.write(self.repo, {"docs/dev.txt": "develop\n"})
            self.commit(self.repo, "develop work")
            self.develop_tip = self.rev()
            _git(self.repo, "update-ref", "refs/remotes/origin/develop", self.develop_tip)
            _git(self.repo, "checkout", "--quiet", "main")
        remote = tmp_path / "state.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
        self.ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
        git_backend.bootstrap(self.ref)
        scope = {member: scope[member] for member in members}
        self.scope = scope
        self.leader = self.service("leader")
        registered = self.leader.register_batch(
            batch_id=BATCH, project_id="tractable", repository=str(self.repo), owner_generation=GEN,
            integration_branch="main", integration_worktree=str(self.repo), start_head=self.start,
            members=members, scope=scope, scope_hash=store.scope_digest(scope),
            delivery_boundary="final-pr", terminal_outcome="merged",
            develop_ref="refs/remotes/origin/develop" if develop else None, confirm_schema3_cutover=True,
        )
        assert registered["ok"], registered
        for member in members:
            made = self.leader.create_item(
                member, member.upper(), needs=self.deps.get(member, []), batch=_meta(member, self.deps.get(member)),
            )
            assert made["ok"], made

    def service(self, worker: str) -> TrackerService:
        return TrackerService(
            ref=self.ref, cache_dir=self.tmp / f"cache-{worker}", worker=worker,
            session_id=f"s-{worker}", client_name="pytest",
        )

    @staticmethod
    def write(root: Path, files: dict[str, str | None]) -> None:
        for name, text in files.items():
            path = root / name
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    @staticmethod
    def commit(root: Path, message: str) -> None:
        _git(root, "add", "-A")
        _git(root, "commit", "--quiet", "-m", message)

    def rev(self, root: Path | None = None) -> str:
        return _git(root or self.repo, "rev-parse", "HEAD")

    def member(self, name: str, files: dict[str, str | None], base: str | None = None) -> tuple[Path, str]:
        path = self.tmp / f"wt-{name}"
        _git(self.repo, "worktree", "add", "--quiet", "-b", f"feat/{name}", str(path), base or self.rev())
        self.write(path, files)
        self.commit(path, f"{name} work")
        return path, self.rev(path)

    def land(self, name: str) -> str:
        _git(self.repo, "merge", "--quiet", "--no-ff", f"feat/{name}", "-m", f"land {name}")
        return self.rev()

    def take(self, name: str, svc: TrackerService | None = None) -> str:
        took = (svc or self.leader).take(name)
        assert took["ok"], took
        return took["data"]["claim"]["generation"]

    def prepare(
        self, name: str, path: Path, accepted: str, base: str, head: str, *,
        svc: TrackerService | None = None, generation: str | None = None,
    ) -> dict[str, Any]:
        return (svc or self.leader).prepare(
            name, generation or self.take(name, svc), batch_id=BATCH, member_id=name,
            source_worktree=str(path), source_revision=accepted, source_base=base, accepted_head=accepted,
            integration_head=head, scope_hash=store.scope_digest(self.scope),
            verification=_verification(accepted), implementation_dependencies=self.deps.get(name, []),
        )

    def land_and_prepare(self, name: str, files: dict[str, str | None], base: str | None = None) -> dict[str, Any]:
        base = base or self.rev()
        generation = self.take(name)
        path, accepted = self.member(name, files, base)
        head = self.land(name)
        return self.prepare(name, path, accepted, base, head, generation=generation)

    def snapshot(self) -> store.Snapshot:
        return git_backend.read(self.ref, self.tmp / "cache-reader").snapshot

    def batch(self) -> dict[str, Any]:
        return self.snapshot().index["batches"][BATCH]

    def tip(self) -> str:
        return subprocess.check_output(
            ["git", "--git-dir", str(self.ref.remote), "rev-parse", self.ref.branch], text=True,
        ).strip()

    def bind(self, head: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.leader.bind_batch_pr(
            BATCH, GEN, pr_repository="o/r", number=1, node_id="PR_1", head=head or self.rev(), **kwargs,
        )

    def evidence(self, acceptance_for: list[str] | None = None, worktree: str | None = None) -> dict[str, Any]:
        snap = self.snapshot()
        batch = snap.index["batches"][BATCH]
        head = self.rev()
        wt = worktree or str(self.repo)
        receipts = {m: snap.details[m]["prepared"] for m in self.members}
        actual = set(TrackerService._changed_paths(wt, self.start, head))
        evidence: dict[str, Any] = {
            "status": "passed", "project_id": "tractable", "repository": str(self.repo), "batch_id": BATCH,
            "tree_worktree": wt, "tree_revision": head, "integration_branch": "main",
            "integration_head": head, "scope_hash": batch["scope_hash"],
            "member_heads": {m: r["accepted_head"] for m, r in receipts.items()},
            "member_ranges": {m: {"base": r["source_base"], "head": r["accepted_head"]} for m, r in receipts.items()},
            "changed_files": sorted(actual - store.excluded_paths(snap, BATCH)),
            "final_pr": batch["final_pr"], "suite": "combined-tree", "clean": True,
        }
        if acceptance_for is not None:
            evidence["current_acceptance"] = {
                m: {
                    "status": "passed", "revision": head, "clean": True, "worktree": wt,
                    "suite": "bounded", "command": ["pytest", "-q"],
                }
                for m in acceptance_for
            }
        return evidence

    def finish(self, name: str, evidence: dict[str, Any], svc: TrackerService | None = None) -> dict[str, Any]:
        svc = svc or self.leader
        return svc.finish(name, self.take(name, svc), evidence)


SCOPE = {"a": ["a/**", "shared/**"], "b": ["b/**", "shared/**", "a/**"], "c": ["c/**", "shared/**"]}


def test_foreign_take_of_active_member_refused_with_owner_code(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    outsider = flow.service("outsider")
    refused = outsider.take("a")
    assert not refused["ok"] and refused["code"] == "E_BATCH_OWNER" and refused["kind"] == "gate"
    assert "leader" in refused["error"] and refused["recovery"]
    refused = outsider.takeover("a", "leader", "thought it was dead")
    assert not refused["ok"] and refused["code"] == "E_BATCH_OWNER"
    took = flow.leader.take("a")
    assert took["ok"], took
    assert flow.snapshot().index["items"]["a"]["claim"]["worker"] == "leader"


def test_synced_member_receipt_lists_only_own_files(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    first = flow.land_and_prepare("a", {"a/f2": "two\n"})
    assert first["ok"], first
    recorded = flow.rev()
    path, accepted = flow.member("b", {"b/f": "b\n"}, base=recorded)
    head = flow.land("b")
    out = flow.prepare("b", path, accepted, recorded, head)
    assert out["ok"], out
    snap = flow.snapshot()
    assert snap.details["b"]["prepared"]["changed_files"] == ["b/f"]
    assert snap.details["a"]["prepared"]["changed_files"] == ["a/f2"]
    assert snap.details["b"]["prepared"]["schema"] == "prepared_work_v2"


def test_synced_member_prepares_from_recorded_integration_head_over_git(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b", "c"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    assert flow.land_and_prepare("b", {"b/f": "b\n"}, base=flow.batch()["integration_head"])["ok"]
    assert flow.land_and_prepare("c", {"c/f": "c\n"}, base=flow.start)["ok"]
    assert [row["kind"] for row in flow.batch()["integration_log"]] == ["start", "prepare", "prepare", "prepare"]


def test_unrecorded_base_refused_over_git(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    _git(flow.repo, "checkout", "--quiet", "-b", "side")
    flow.write(flow.repo, {"unrecorded": "x\n"})
    flow.commit(flow.repo, "unrecorded")
    unrecorded = flow.rev()
    _git(flow.repo, "checkout", "--quiet", "main")
    path, accepted = flow.member("a", {"a/f2": "two\n"}, base=unrecorded)
    head = flow.land("a")
    refused = flow.prepare("a", path, accepted, unrecorded, head)
    assert not refused["ok"] and refused["code"] == "E_BATCH_HEAD"
    assert "prepared" not in flow.snapshot().details["a"]


def test_path_outside_member_scope_still_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], {"a": ["a/**"]})
    path, accepted = flow.member("a", {"a/f2": "two\n", "outside/f": "x\n"})
    head = flow.land("a")
    refused = flow.prepare("a", path, accepted, flow.start, head)
    assert not refused["ok"] and refused["code"] == "E_STATE"
    assert "frozen scope" in refused["error"]


def test_fix_advance_recorded_and_becomes_dispatch_base(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    landed = flow.rev()
    flow.write(flow.repo, {"a/f2": "two fixed\n"})
    flow.commit(flow.repo, "review fix")
    fixed = flow.rev()
    advanced = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=landed, head=fixed, reason="fix", changed_files=["a/f2"],
    )
    assert advanced["ok"], advanced
    assert advanced["data"]["requires_acceptance"] == ["a"] and advanced["data"]["idempotent"] is False
    batch = flow.batch()
    assert batch["integration_head"] == fixed
    entry = batch["integration_log"][-1]
    assert (entry["kind"], entry["changed_files"], entry["previous_head"]) == ("fix", ["a/f2"], landed)
    out = flow.land_and_prepare("b", {"b/f": "b\n"}, base=fixed)
    assert out["ok"], out
    shown = flow.leader.show_batch(BATCH)["data"]
    assert shown["integration_log"][2]["kind"] == "fix"
    assert shown["acceptance_required"]["a"][0]["kind"] == "fix"


def test_advance_stale_expected_head_refused_over_git(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    flow.write(flow.repo, {"a/f1": "fixed\n"})
    flow.commit(flow.repo, "fix")
    fixed = flow.rev()
    assert flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=flow.start, head=fixed, reason="fix", changed_files=["a/f1"],
    )["ok"]
    flow.write(flow.repo, {"a/f1": "fixed again\n"})
    flow.commit(flow.repo, "fix 2")
    refused = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=flow.start, head=flow.rev(), reason="fix", changed_files=["a/f1"],
    )
    assert not refused["ok"] and refused["code"] == "E_CONFLICT"
    assert flow.batch()["integration_head"] == fixed


def test_advance_non_descendant_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    flow.write(flow.repo, {"a/f1": "fixed\n"})
    flow.commit(flow.repo, "fix")
    first = flow.rev()
    assert flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=flow.start, head=first, reason="fix", changed_files=["a/f1"],
    )["ok"]
    _git(flow.repo, "reset", "--quiet", "--hard", flow.start)
    flow.write(flow.repo, {"a/f1": "diverged\n"})
    flow.commit(flow.repo, "diverged")
    refused = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=first, head=flow.rev(), reason="fix", changed_files=["a/f1"],
    )
    assert not refused["ok"] and refused["code"] == "E_BATCH_HEAD"
    assert flow.batch()["integration_head"] == first


def test_advance_non_owner_refused_over_git(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    flow.write(flow.repo, {"a/f1": "fixed\n"})
    flow.commit(flow.repo, "fix")
    refused = flow.service("outsider").advance_batch_head(
        BATCH, GEN, expected_head=flow.start, head=flow.rev(), reason="fix", changed_files=["a/f1"],
    )
    assert not refused["ok"] and refused["code"] == "E_CLAIM_STALE"
    wrong = flow.leader.advance_batch_head(
        BATCH, "9" * 32, expected_head=flow.start, head=flow.rev(), reason="fix", changed_files=["a/f1"],
    )
    assert not wrong["ok"] and wrong["code"] == "E_CLAIM_STALE"


def test_advance_exact_retry_idempotent(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    flow.write(flow.repo, {"a/f1": "fixed\n"})
    flow.commit(flow.repo, "fix")
    args = dict(expected_head=flow.start, head=flow.rev(), reason="fix", changed_files=["a/f1"])
    assert flow.leader.advance_batch_head(BATCH, GEN, **args)["data"]["idempotent"] is False
    tip = flow.tip()
    again = flow.leader.advance_batch_head(BATCH, GEN, **args)
    assert again["ok"] and again["data"]["idempotent"] is True and "_no_write" not in again["data"]
    assert flow.tip() == tip
    assert len(flow.batch()["integration_log"]) == 2
    wrong_files = flow.leader.advance_batch_head(BATCH, GEN, **{**args, "changed_files": ["a/other"]})
    assert not wrong_files["ok"] and wrong_files["code"] == "E_CONFLICT"


def test_advance_changed_files_must_match_the_actual_diff(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    flow.write(flow.repo, {"a/f1": "fixed\n"})
    flow.commit(flow.repo, "fix")
    refused = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=flow.start, head=flow.rev(), reason="fix", changed_files=["shared/f"],
    )
    assert not refused["ok"] and refused["code"] == "E_STATE"


def test_unrecorded_commit_before_prepare_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    base = flow.rev()
    path, accepted = flow.member("b", {"b/f": "b\n"}, base=base)
    flow.write(flow.repo, {"a/f2": "sneaky leader fix\n"})
    flow.commit(flow.repo, "unrecorded fix")
    head = flow.land("b")
    refused = flow.prepare("b", path, accepted, base, head)
    assert not refused["ok"] and refused["code"] == "E_BATCH_HEAD"
    assert "advance_batch_head" in " ".join(refused["recovery"])


def test_same_file_evolution_over_git_records_overlap_and_final_requires_acceptance(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"shared/f": "line A\n"})["ok"]
    out = flow.land_and_prepare("b", {"shared/f": "line B\n"}, base=flow.rev())
    assert out["ok"], out
    assert out["data"]["prepared"]["overlaps"] == [{"kind": "member", "member": "a", "paths": ["shared/f"]}]
    assert flow.bind()["ok"]
    refused = flow.finish("b", flow.evidence())
    assert not refused["ok"] and refused["code"] == "E_FINAL_EVIDENCE" and "'a'" in refused["error"]
    finished = flow.finish("b", flow.evidence(["a"]))
    assert finished["ok"], finished


def test_silent_revert_over_git_cannot_reach_passing_evidence(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"shared/f": "line\nA's new line\n"})["ok"]
    reverted = flow.land_and_prepare("b", {"shared/f": "line\n"}, base=flow.rev())
    assert reverted["ok"]
    assert flow.bind()["ok"]
    for member in ("a", "b"):
        refused = flow.finish(member, flow.evidence())
        assert not refused["ok"] and refused["code"] == "E_FINAL_EVIDENCE"
        assert flow.leader.release(member, flow.snapshot().index["items"][member]["claim"]["generation"])["ok"]
    assert flow.finish("b", flow.evidence(["a"]))["ok"]


def test_rename_of_earlier_member_file_records_overlap(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f1": "one edited\n"})["ok"]
    base = flow.rev()
    path = tmp_path / "wt-b"
    _git(flow.repo, "worktree", "add", "--quiet", "-b", "feat/b", str(path), base)
    _git(path, "mv", "a/f1", "a/renamed")
    flow.commit(path, "rename")
    accepted = flow.rev(path)
    head = flow.land("b")
    out = flow.prepare("b", path, accepted, base, head)
    assert out["ok"], out
    receipt = flow.snapshot().details["b"]["prepared"]
    assert receipt["changed_files"] == ["a/f1", "a/renamed"]
    assert out["data"]["prepared"]["overlaps"] == [{"kind": "member", "member": "a", "paths": ["a/f1"]}]


def test_develop_merge_over_git_is_excluded_and_never_hides_member_changes(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], {"a": ["a/**"]}, develop=True)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    before = flow.rev()
    _git(flow.repo, "merge", "--quiet", "--no-ff", "refs/remotes/origin/develop", "-m", "merge develop")
    merged = flow.rev()
    advanced = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=before, head=merged, reason="develop-merge", changed_files=["docs/dev.txt"],
    )
    assert advanced["ok"], advanced
    entry = flow.batch()["integration_log"][-1]
    assert entry["kind"] == "develop-merge" and entry["merged_parent"] == flow.develop_tip
    assert flow.bind()["ok"]
    evidence = flow.evidence()
    assert "docs/dev.txt" not in evidence["changed_files"] and evidence["changed_files"] == ["a/f2"]
    assert flow.finish("a", evidence)["ok"]


def test_develop_merge_with_parent_outside_registered_develop_ref_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], {"a": ["a/**"]}, develop=True)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    before = flow.rev()
    _git(flow.repo, "checkout", "--quiet", "-b", "feature-side", flow.start)
    flow.write(flow.repo, {"docs/sneaky.txt": "not develop\n"})
    flow.commit(flow.repo, "side work")
    _git(flow.repo, "checkout", "--quiet", "main")
    _git(flow.repo, "merge", "--quiet", "--no-ff", "feature-side", "-m", "merge side")
    refused = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=before, head=flow.rev(), reason="develop-merge",
        changed_files=["docs/sneaky.txt"],
    )
    assert not refused["ok"] and refused["code"] == "E_BATCH_HEAD"
    assert flow.batch()["integration_head"] == before


def test_develop_merge_must_be_a_real_merge_of_content_from_the_merged_side(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], {"a": ["a/**"]}, develop=True)
    before = flow.rev()
    _git(flow.repo, "merge", "--quiet", "--ff-only", "refs/remotes/origin/develop")
    refused = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=before, head=flow.rev(), reason="develop-merge", changed_files=["docs/dev.txt"],
    )
    assert not refused["ok"] and refused["code"] == "E_BATCH_HEAD"
    _git(flow.repo, "reset", "--quiet", "--hard", before)
    _git(flow.repo, "merge", "--quiet", "--no-ff", "refs/remotes/origin/develop", "-m", "merge develop")
    flow.write(flow.repo, {"a/smuggled": "member content\n"})
    flow.commit(flow.repo, "smuggle into the merge")
    refused = flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=before, head=flow.rev(), reason="develop-merge",
        changed_files=["a/smuggled", "docs/dev.txt"],
    )
    assert not refused["ok"]


def test_rebind_over_git_is_idempotent_without_a_commit(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    first = flow.rev()
    assert flow.bind()["data"]["idempotent"] is False
    tip = flow.tip()
    again = flow.bind()
    assert again["ok"] and again["data"]["idempotent"] is True and flow.tip() == tip
    flow.write(flow.repo, {"a/f2": "fix\n"})
    flow.commit(flow.repo, "fix")
    fixed = flow.rev()
    assert flow.leader.advance_batch_head(
        BATCH, GEN, expected_head=first, head=fixed, reason="fix", changed_files=["a/f2"],
    )["ok"]
    moved = flow.bind(fixed, expected_previous_head=first)
    assert moved["ok"] and moved["data"]["previous_head"] == first, moved
    assert flow.batch()["final_pr"]["head"] == fixed
    different = flow.leader.bind_batch_pr(
        BATCH, GEN, pr_repository="o/r", number=2, node_id="PR_2", head=fixed, expected_previous_head=fixed,
    )
    assert not different["ok"] and different["code"] == "E_CONFLICT"


def test_recover_over_git_audits_members_and_marks_previous_generation_stale(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    rescuer = flow.service("rescuer")
    wrong = rescuer.recover_batch_owner(BATCH, "nobody", GEN, "3" * 32, "leader died")
    assert not wrong["ok"] and wrong["code"] == "E_CONFLICT"
    stale = rescuer.recover_batch_owner(BATCH, "leader", "9" * 32, "3" * 32, "leader died")
    assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE"
    sessionless = TrackerService(ref=flow.ref, cache_dir=tmp_path / "cache-x", worker="rescuer")
    assert sessionless.recover_batch_owner(BATCH, "leader", GEN, "3" * 32, "leader died")["code"] == "E_STATE"
    out = rescuer.recover_batch_owner(BATCH, "leader", GEN, "3" * 32, "leader died")
    assert out["ok"], out
    assert out["data"]["owner"] == "rescuer" and out["data"]["owner_epoch"] == 1
    assert "owner_generation" not in out["data"] and "previous_owner" not in out["data"]
    snap = flow.snapshot()
    for member in ("a", "b"):
        entry = snap.details[member]["sessions"][-1]
        assert entry["op"] == "recover" and entry["actor"] == "rescuer"
        assert entry["displaced"] == "leader" and entry["reason"] == "leader died"
    tip = flow.tip()
    retry = rescuer.recover_batch_owner(BATCH, "leader", GEN, "3" * 32, "leader died")
    assert retry["ok"] and retry["data"]["idempotent"] is True and flow.tip() == tip
    assert not flow.leader.abort_batch(BATCH, GEN)["ok"]
    assert not flow.leader.take("b")["ok"]
    took = rescuer.take("b")
    assert took["ok"]
    assert rescuer.release("b", took["data"]["claim"]["generation"])["ok"]
    assert rescuer.abort_batch(BATCH, "3" * 32)["ok"]


def test_recovered_owner_takes_over_a_claimed_member_with_a_prepared_dependency(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE, deps={"b": ["a"]})
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    base = flow.rev()
    flow.take("b")
    assert flow.leader.take("b")["ok"]
    rescuer = flow.service("rescuer")
    assert rescuer.recover_batch_owner(BATCH, "leader", GEN, "3" * 32, "leader died")["ok"]
    took = rescuer.takeover("b", "leader", "leader session ended with its host")
    assert took["ok"], took
    path, accepted = flow.member("b", {"b/f": "b\n"}, base)
    head = flow.land("b")
    generation = took["data"]["claim"]["generation"]
    assert flow.prepare("b", path, accepted, base, head, svc=rescuer, generation=generation)["ok"]


def test_takeover_of_a_member_whose_dependency_receipt_is_missing_is_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE, deps={"b": ["a"]})
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    flow.take("b")
    rescuer = flow.service("rescuer")
    assert rescuer.recover_batch_owner(BATCH, "leader", GEN, "3" * 32, "leader died")["ok"]
    assert rescuer.update_item("a", title="A changed")["ok"]
    refused = rescuer.takeover("b", "leader", "leader session ended with its host")
    assert not refused["ok"] and refused["code"] == "E_NOTHING_READY", refused


def test_recover_over_git_with_relocation_updates_integration_worktree(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    prep_a = flow.land_and_prepare("a", {"a/f2": "two\n"})
    assert prep_a["ok"]
    head_after_a = flow.rev()
    receipt_a_before = flow.snapshot().details["a"]["prepared"]

    dest = tmp_path / "dest-repo"
    _git(flow.repo, "checkout", "--quiet", "--detach")
    subprocess.run(["git", "-C", str(flow.repo), "worktree", "add", str(dest), "main"], check=True)

    rescuer = flow.service("rescuer")
    relocated = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "leader dead, relocating",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head=head_after_a,
        integration_worktree=str(dest),
    )
    assert relocated["ok"], relocated
    data = relocated["data"]
    assert data["owner"] == "rescuer" and data["owner_epoch"] == 1
    assert data["relocation"]["previous_integration_worktree"] == str(flow.repo)
    assert data["relocation"]["integration_worktree"] == str(dest)

    batch = flow.batch()
    assert batch["integration_worktree"] == str(dest)
    assert batch["owner"] == "rescuer"

    # Prepared receipts unchanged byte-for-byte
    receipt_a_after = flow.snapshot().details["a"]["prepared"]
    assert receipt_a_after == receipt_a_before

    # Idempotent retry with the same new generation and same relocation tuple succeeds
    retry = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "leader dead, relocating",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head=head_after_a,
        integration_worktree=str(dest),
    )
    assert retry["ok"] and retry["data"]["idempotent"] is True

    # Rescuer can now land and prepare member b using the new worktree!
    b_branch = "feat-b"
    _git(dest, "branch", b_branch, head_after_a)
    b_worktree = tmp_path / "member-b"
    subprocess.run(["git", "-C", str(dest), "worktree", "add", str(b_worktree), b_branch], check=True)
    flow.write(b_worktree, {"b/f2": "b-two\n"})
    flow.commit(b_worktree, "b work")
    b_head = subprocess.run(["git", "-C", str(b_worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    _git(dest, "merge", "--quiet", "--no-ff", b_head, "-m", "merge b")
    merge_head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    took = rescuer.take("b")
    assert took["ok"]
    prep_b = rescuer.prepare(
        "b", took["data"]["claim"]["generation"],
        batch_id=BATCH, member_id="b",
        source_worktree=str(b_worktree), source_revision=b_head,
        source_base=head_after_a, accepted_head=b_head,
        integration_head=merge_head,
        scope_hash=store.scope_digest(flow.scope),
        verification={
            "status": "passed", "clean": True, "suite": "bounded", "command": ["pytest", "-q"],
            "revision": b_head, "review": {"outcome": "clear"},
        },
    )
    assert prep_b["ok"], prep_b


def test_recover_over_git_with_relocation_negatives(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    current_head = flow.rev()
    dest = tmp_path / "dest-repo"
    _git(flow.repo, "checkout", "--quiet", "--detach")
    subprocess.run(["git", "-C", str(flow.repo), "worktree", "add", str(dest), "main"], check=True)

    rescuer = flow.service("rescuer")

    # Incomplete relocation tuple
    inc = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "reason",
        expected_integration_worktree=str(flow.repo),
    )
    assert not inc["ok"] and inc["code"] == "E_STATE"

    # Wrong expected worktree
    wrong_wt = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "reason",
        expected_integration_worktree=str(tmp_path / "wrong"),
        expected_integration_head=current_head,
        integration_worktree=str(dest),
    )
    assert not wrong_wt["ok"] and wrong_wt["code"] == "E_CONFLICT"

    # Wrong expected head
    wrong_head = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "reason",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head="0" * 40,
        integration_worktree=str(dest),
    )
    assert not wrong_head["ok"] and wrong_head["code"] == "E_CONFLICT"

    # Dirty destination worktree
    flow.write(dest, {"dirty.txt": "dirty\n"})
    dirty = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "reason",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head=current_head,
        integration_worktree=str(dest),
    )
    assert not dirty["ok"] and dirty["code"] == "E_STATE"
    (dest / "dirty.txt").unlink()

    # Successful relocation
    ok_reloc = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "reason",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head=current_head,
        integration_worktree=str(dest),
    )
    assert ok_reloc["ok"]

    # Retry mismatch (different dest with same new generation)
    other_dest = tmp_path / "other-dest"
    _git(dest, "checkout", "--quiet", "--detach")
    subprocess.run(["git", "-C", str(flow.repo), "worktree", "add", str(other_dest), "main"], check=True)
    mismatch = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "reason",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head=current_head,
        integration_worktree=str(other_dest),
    )
    assert not mismatch["ok"] and mismatch["code"] == "E_CONFLICT"


def test_recover_over_git_with_relocation_active_partial_closeout(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    assert flow.land_and_prepare("b", {"b/f1": "three\n"})["ok"]
    final_head = flow.rev()
    assert flow.bind(final_head)["ok"]

    # Member a is finished
    ev_before = flow.evidence(acceptance_for=["a", "b"])
    a_fin = flow.finish("a", ev_before)
    assert a_fin["ok"], a_fin

    dest = tmp_path / "dest-repo"
    _git(flow.repo, "checkout", "--quiet", "--detach")
    subprocess.run(["git", "-C", str(flow.repo), "worktree", "add", str(dest), "main"], check=True)

    rescuer = flow.service("rescuer")
    reloc = rescuer.recover_batch_owner(
        BATCH, "leader", GEN, "3" * 32, "relocate during closeout",
        expected_integration_worktree=str(flow.repo),
        expected_integration_head=final_head,
        integration_worktree=str(dest),
    )
    assert reloc["ok"], reloc

    # Old-location evidence fails at new location
    old_ev = flow.evidence(acceptance_for=["a", "b"])
    b_took = rescuer.take("b")
    assert b_took["ok"]
    old_fail = rescuer.finish("b", b_took["data"]["claim"]["generation"], final_evidence=old_ev)
    assert not old_fail["ok"] and old_fail["code"] == "E_FINAL_EVIDENCE"

    # Fresh new-location evidence succeeds!
    new_ev = flow.evidence(acceptance_for=["a", "b"], worktree=str(dest))
    b_fin = rescuer.finish("b", b_took["data"]["claim"]["generation"], final_evidence=new_ev)
    assert b_fin["ok"], b_fin
    assert flow.batch()["lifecycle"] == "completed"
def test_show_batch_never_serves_owner_generation(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    shown = flow.service("anyone").show_batch(BATCH)
    assert shown["ok"], shown
    assert GEN not in json.dumps(shown)
    data = shown["data"]
    assert data["owner"] == "leader" and data["owner_epoch"] == 0
    assert data["capability"]["version"] == 2 and data["store"]["schema_version"] == 4
    assert data["members"][0]["prepared"] is True and data["receipts"]["a"]["changed_files"] == ["a/f2"]
    unknown = flow.leader.show_batch("nope")
    assert not unknown["ok"] and unknown["code"] == "E_STATE"


def test_show_batch_pages_oversized_section(tmp_path: Path) -> None:
    svc, ref = _bulk_service(tmp_path)
    shown = svc.show_batch(BATCH)
    assert shown["ok"], shown
    section = shown["data"]["sections"]["receipts"]
    assert section["bytes"] > 16 * 1024 and "continuation" in section
    assert "receipts" not in shown["data"] and shown["data"]["members"]
    assert len(json.dumps(shown, indent=2, sort_keys=True).encode()) <= 16 * 1024
    chunks: list[str] = []
    offset, rev = 0, shown["data"]["rev"]
    while True:
        page = svc.show_batch(BATCH, field="receipts", offset=offset, budget=5000, rev=rev if offset else None)
        assert page["ok"], page
        chunks.append(page["data"]["window"])
        if "continuation" not in page["data"]:
            break
        offset = page["data"]["continuation"]["offset"]
    assert len(chunks) > 2
    receipts = json.loads("".join(chunks))
    assert sorted(receipts) == sorted(f"m{i}" for i in range(12))
    stale = svc.show_batch(BATCH, field="receipts", offset=5000)
    assert not stale["ok"] and stale["code"] == "E_CURSOR_STALE"
    moved = svc.show_batch(BATCH, field="receipts", offset=5000, rev="0" * 40)
    assert not moved["ok"] and moved["code"] == "E_CURSOR_STALE"
    assert not svc.show_batch(BATCH, field="bogus")["ok"]


def _bulk_service(tmp_path: Path, count: int = 12) -> tuple[TrackerService, git_backend.StateRef]:
    remote = tmp_path / f"bulk{count}.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    members = [f"m{i}" for i in range(count)]

    def seed(snap: store.Snapshot) -> dict[str, Any]:
        scope = {m: [f"{m}/**"] for m in members}
        store.op_register_batch(
            snap, batch_id=BATCH, project_id="p", repository="repo", owner="leader", owner_generation=GEN,
            integration_branch="main", integration_worktree="/tmp/integration", start_head=h("start"),
            members=members, scope=scope, scope_hash=store.scope_digest(scope),
            delivery_boundary="final-pr", terminal_outcome="merged", confirm_schema3_cutover=True,
        )
        for member in members:
            store.op_create(snap, item_id=member, title=member, batch=_meta(member))
            generation = store.op_take(snap, member, "leader")["claim"]["generation"]
            accepted = h(f"acc-{member}")
            verification = _verification(accepted)
            verification["command"] = ["pytest", "-q", *[f"tests/test_{i}.py::{'x' * 40}" for i in range(30)]]
            store.op_prepare(
                snap, member, "leader", generation, batch_id=BATCH, member_id=member, source_worktree="/tmp/src",
                source_revision=accepted, source_base=h("start"), accepted_head=accepted,
                integration_head=h(f"head-{member}"), scope_hash=snap.index["batches"][BATCH]["scope_hash"],
                verification=verification,
            )
        return {"id": "seed"}

    assert git_backend.mutate(ref, op="update", summary="seed bulk", worker="seeder", apply=seed).ok
    return TrackerService(ref=ref, cache_dir=tmp_path / f"cache-bulk{count}", worker="reader"), ref


def test_cli_batch_show_matches_tool_and_never_writes(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    tip = flow.tip()
    command = [
        sys.executable, "-m", "tractable.cli", "batch", "show", BATCH,
        "--state-remote", str(flow.ref.remote), "--state-branch", "todo-state",
        "--cache-dir", str(tmp_path / "cache-cli"),
    ]
    done = subprocess.run(command, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    from_cli = json.loads(done.stdout)
    from_tool = flow.service("reader").show_batch(BATCH)
    assert from_cli == from_tool
    assert flow.tip() == tip
    missing = subprocess.run([*command[:5], "nope", *command[6:]], capture_output=True, text=True)
    assert missing.returncode == 2 and json.loads(missing.stdout)["code"] == "E_STATE"
    paged = subprocess.run(
        [*command, "--field", "receipts", "--budget", "200"], capture_output=True, text=True,
    )
    assert paged.returncode == 0 and json.loads(paged.stdout)["data"]["continuation"]["offset"] == 200
    assert flow.tip() == tip


def test_owner_reservation_survives_a_real_prepare_flow(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE, deps={"b": ["a"]})
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    out = flow.land_and_prepare("b", {"b/f": "b\n"})
    assert out["ok"], out
    assert flow.bind()["ok"]
    assert flow.finish("a", flow.evidence())["ok"]
    assert flow.finish("b", flow.evidence())["ok"]
    assert flow.batch()["lifecycle"] == "completed"


# ------------------------------------------------- review fixes (ADR 0012)


def test_silent_revert_blocked_after_invalidate_then_reprepare() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["shared/f"])
    store.op_update(snap, "a", description="clarified")
    assert "prepared" not in snap.details["a"]
    accepted = snap.index["batches"][BATCH]["accepted_members"]["a"]
    out_b = _prep(snap, "b", base=_head(snap), files=["shared/f"])
    assert out_b["prepared"]["overlaps"] == [{"kind": "member", "member": "a", "paths": ["shared/f"]}]
    _prep(snap, "a", base=h("start"), head=_head(snap), files=["shared/f"], accepted=accepted)
    assert snap.index["batches"][BATCH]["integrated_members"]["a"]["changed_files"] == ["shared/f"]
    required = store.acceptance_required(snap, BATCH)
    assert sorted(required) == ["a", "b"]
    assert required["a"][0]["by"] == "b"
    store.validate_snapshot(snap.index, snap.details)
    _bind(snap)
    with pytest.raises(TodoError) as exc:
        _finish(snap, "a", _final(snap, ["a", "b"], _accept(snap, ["b"])))
    assert exc.value.code == "E_FINAL_EVIDENCE" and "'a'" in str(exc.value)
    with pytest.raises(TodoError) as exc:
        _finish(snap, "a", _final(snap, ["a", "b"]))
    assert exc.value.code == "E_FINAL_EVIDENCE"
    snap.index["items"]["a"]["claim"] = None
    snap.index["items"]["a"]["status"] = "open"
    _finish(snap, "a", _final(snap, ["a", "b"], _accept(snap, ["a", "b"])))
    assert snap.index["items"]["a"]["status"] == "done"


def test_any_edit_of_done_prepared_member_is_refused() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a")
    _prep(snap, "b", base=_head(snap))
    _bind(snap)
    _finish(snap, "a", _final(snap, ["a", "b"]))
    for kwargs in ({"context_append": "note"}, {"description": "late edit"}):
        with pytest.raises(TodoError) as exc:
            store.op_update(snap, "a", **kwargs)
        assert exc.value.code == "E_STATE" and exc.value.recovery
    assert "prepared" in snap.details["a"]
    assert "note" not in snap.details["a"].get("context", "")


def _overlap_snap() -> store.Snapshot:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["shared/f"])
    _prep(snap, "b", base=_head(snap), files=["shared/f"])
    _bind(snap)
    return snap


@pytest.mark.parametrize(
    "field,value",
    [("worktree", "/tmp/elsewhere"), ("suite", "weaker-suite"), ("clean", False), ("status", "failed")],
)
def test_current_acceptance_mismatch_refused(field: str, value: Any) -> None:
    snap = _overlap_snap()
    acceptance = _accept(snap, ["a"])
    acceptance["a"][field] = value
    with pytest.raises(TodoError) as exc:
        _finish(snap, "b", _final(snap, ["a", "b"], acceptance))
    assert exc.value.code == "E_FINAL_EVIDENCE" and exc.value.recovery
    snap.index["items"]["b"]["claim"] = None
    snap.index["items"]["b"]["status"] = "open"
    _finish(snap, "b", _final(snap, ["a", "b"], _accept(snap, ["a"])))


def test_lacking_acceptance_refusal_has_recovery() -> None:
    snap = _overlap_snap()
    with pytest.raises(TodoError) as exc:
        _finish(snap, "b", _final(snap, ["a", "b"]))
    assert exc.value.code == "E_FINAL_EVIDENCE" and exc.value.recovery


def _refused(call: Any, code: str) -> None:
    with pytest.raises(TodoError) as exc:
        call()
    assert exc.value.code == code, (exc.value.code, str(exc.value))
    assert exc.value.recovery, f"{code} refusal has no recovery steps: {exc.value}"


def test_every_new_store_refusal_carries_recovery_steps() -> None:
    snap = _pure(["a", "b"], develop_ref="refs/remotes/origin/develop")
    start = _head(snap)

    def advance(**kw: Any) -> dict[str, Any]:
        params: dict[str, Any] = dict(expected_head=_head(snap), head=h("f1"), reason="fix", changed_files=["a/f"])
        params.update(kw)
        return store.op_advance_batch_head(snap, BATCH, "leader", GEN, **params)

    _refused(lambda: advance(expected_head=h("stale")), "E_CONFLICT")
    _refused(lambda: advance(head=start), "E_BATCH_HEAD")
    _refused(lambda: advance(changed_files=["outside/f"]), "E_BATCH_HEAD")
    _refused(lambda: advance(reason="develop-merge", changed_files=["docs/x"]), "E_BATCH_HEAD")
    _prep(snap, "a", files=["a/f"])
    _refused(lambda: _prep(snap, "b", base=_head(snap), head=start), "E_BATCH_HEAD")
    _refused(lambda: _prep(snap, "b", base=h("nowhere")), "E_BATCH_HEAD")
    _refused(lambda: _bind(snap, expected_previous_head=h("x")), "E_CONFLICT")
    _refused(lambda: _bind(snap, head=h("other")), "E_CONFLICT")
    assert _bind(snap)["idempotent"] is False
    _refused(lambda: _bind(snap, number=2, node="PR_2"), "E_CONFLICT")
    _refused(lambda: _bind(snap, expected_previous_head=h("x")), "E_CONFLICT")
    advance()
    _refused(lambda: _bind(snap), "E_CONFLICT")
    _refused(lambda: _bind(snap, expected_previous_head=h("x")), "E_CONFLICT")
    _refused(lambda: _bind(snap, head=h("other"), expected_previous_head=start), "E_CONFLICT")
    _refused(lambda: _recover(snap, expected_owner="someone-else"), "E_CONFLICT")
    _refused(lambda: _recover(snap, expected_owner_generation="9" * 32), "E_CLAIM_STALE")
    _refused(lambda: store.op_update(snap, "a", batch=_meta("a", ["b"])), "E_STATE")
    _refused(lambda: advance(reason="bogus"), "E_STATE")
    _refused(lambda: _recover(snap, new_owner_generation="short"), "E_STATE")
    _refused(lambda: _recover(snap, new_owner_generation=GEN), "E_STATE")
    _refused(
        lambda: store.op_advance_batch_head(
            snap, BATCH, "leader", "9" * 32, expected_head=_head(snap), head=h("f9"), reason="fix",
            changed_files=["a/f"],
        ),
        "E_CLAIM_STALE",
    )
    _refused(lambda: _final_acceptance_refusal(snap, {"a": "bad"}, ["a"]), "E_FINAL_EVIDENCE")
    _refused(lambda: _final_acceptance_refusal(snap, "not-an-object", ["a"]), "E_FINAL_EVIDENCE")
    _refused(lambda: _final_acceptance_refusal(snap, {"ghost": {}}, ["a"]), "E_FINAL_EVIDENCE")


def _final_acceptance_refusal(snap: store.Snapshot, acceptance: Any, members: list[str]) -> None:
    evidence = _final(snap, members)
    evidence["current_acceptance"] = acceptance
    batch = snap.index["batches"][BATCH]
    store._validate_current_acceptance(snap, members[0], BATCH, batch, evidence)


def test_final_tree_missing_a_recorded_head_refused_with_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    assert flow.land_and_prepare("a", {"a/f2": "two\n"})["ok"]
    assert flow.bind()["ok"]
    evidence = flow.evidence()
    generation = flow.take("a")
    monkeypatch.setattr(TrackerService, "_is_ancestor", staticmethod(lambda *args: False))
    done = flow.leader.finish("a", generation, evidence)
    assert not done["ok"] and done["code"] == "E_FINAL_EVIDENCE" and done["recovery"], done

    def boom(*args: Any) -> bool:
        raise TodoError("git failed", code="E_STATE")

    monkeypatch.setattr(TrackerService, "_is_ancestor", staticmethod(boom))
    done = flow.leader.finish("a", generation, evidence)
    assert not done["ok"] and done["code"] == "E_FINAL_EVIDENCE" and done["recovery"], done


def _flip_to_pre_v2(snap: store.Snapshot) -> None:
    snap.index["schema_version"] = 3
    snap.index["batches"][BATCH]["batch_schema"] = 1


def test_v2_batch_operations_refused_on_pre_v2_batch_with_recovery() -> None:
    snap = _pure(["a"])
    store.op_take(snap, "a", "leader")
    _flip_to_pre_v2(snap)
    _refused(lambda: _prep(snap, "a"), "E_SCHEMA")
    _refused(
        lambda: store.op_advance_batch_head(
            snap, BATCH, "leader", GEN, expected_head=_head(snap), head=h("f"), reason="fix", changed_files=["a/f"],
        ),
        "E_SCHEMA",
    )
    _refused(lambda: _recover(snap), "E_SCHEMA")
    _refused(lambda: _bind(snap), "E_SCHEMA")


def test_show_batch_receipts_never_serve_owner_generation_for_v1_receipts() -> None:
    snap = _pure(["a", "b"])
    _prep(snap, "a", files=["a/f"])
    _prep(snap, "b", base=_head(snap), files=["b/f"])
    snap.details["a"]["prepared"]["owner_generation"] = GEN
    service = object.__new__(TrackerService)
    section = service._batch_section(snap, BATCH, "receipts")
    assert sorted(section) == ["a", "b"] and GEN not in json.dumps(section)
    assert snap.details["a"]["prepared"]["owner_generation"] == GEN


@pytest.mark.parametrize("count", [1, 8, 16, 24, 29, 33, 40])
def test_show_batch_default_response_fits_the_cap_at_every_member_count(tmp_path: Path, count: int) -> None:
    svc, _ = _bulk_service(tmp_path, count)
    shown = svc.show_batch(BATCH)
    assert shown["ok"], shown
    assert len(json.dumps(shown, indent=2, sort_keys=True).encode()) <= 16 * 1024
    for name, section in shown["data"]["sections"].items():
        assert name in shown["data"] or "continuation" in section
    receipts: list[str] = []
    offset, rev = 0, shown["data"]["rev"]
    while "receipts" not in shown["data"]:
        page = svc.show_batch(BATCH, field="receipts", offset=offset, budget=4000, rev=rev if offset else None)
        assert page["ok"], page
        receipts.append(page["data"]["window"])
        if "continuation" not in page["data"]:
            break
        offset = page["data"]["continuation"]["offset"]
    if receipts:
        assert len(json.loads("".join(receipts))) == count


def test_show_batch_summary_read_pins_rev_and_oversized_window_is_refused(tmp_path: Path) -> None:
    svc, _ = _bulk_service(tmp_path)
    current = svc.show_batch(BATCH)["data"]["rev"]
    assert svc.show_batch(BATCH, rev=current)["ok"]
    moved = svc.show_batch(BATCH, rev="0" * 40)
    assert not moved["ok"] and moved["code"] == "E_CURSOR_STALE" and moved["recovery"]
    big = svc.show_batch(BATCH, field="receipts", budget=10**9)
    assert not big["ok"] and big["code"] == "E_OVERSIZED" and big["recovery"]


def test_finishing_a_member_of_an_active_pre_v2_batch_is_refused_with_e_schema(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a"], SCOPE)
    generation = flow.take("a")

    def flip(snap: store.Snapshot) -> dict[str, Any]:
        snap.index["batches"][BATCH]["batch_schema"] = 1
        return {"id": "flip"}

    assert git_backend.mutate(flow.ref, op="update", summary="flip", worker="seeder", apply=flip).ok
    for evidence in (None, {}):
        done = flow.leader.finish("a", generation, evidence)
        assert not done["ok"] and done["code"] == "E_SCHEMA" and done["recovery"], done
