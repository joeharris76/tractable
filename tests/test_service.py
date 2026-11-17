"""Service-level proving cases: budgets, paging, sections, lifecycle."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tractable import git_backend
from tractable import store as S
from tractable.errors import TodoError
from tractable.service import MAX_BYTES, TrackerService, count_tokens


def _svc(tmp_path: Path, worker: str = "w1", name: str = "s.git") -> TrackerService:
    remote = tmp_path / name
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    return TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker=worker)


def _size(env: dict) -> int:
    return len(json.dumps(env, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode())


def test_service_carries_session_context_and_rejects_forgeries(tmp_path: Path) -> None:
    from tractable.errors import TodoError

    remote = tmp_path / "sctx.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    svc = TrackerService(
        ref=ref, cache_dir=tmp_path / "cache", worker="w1",
        session_id="session-a", client_name="claude-code",
    )
    assert svc.session_id == "session-a"
    assert svc.client_name == "claude-code"
    legacy = TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1")
    assert legacy.session_id is None and legacy.client_name is None
    with pytest.raises(TodoError):
        TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1", session_id="bad\nline")
    with pytest.raises(TodoError):
        TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1", client_name="x" * 65)


def _session_svc(tmp_path: Path, worker: str = "w1", session: str = "session-a"):
    remote = tmp_path / "sess.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    svc = TrackerService(
        ref=ref, cache_dir=tmp_path / "cache", worker=worker,
        session_id=session, client_name="pytest",
    )
    return svc, ref


def _sessions_of(ref, cache: Path):
    snap = git_backend.read(ref, cache).snapshot
    return snap.details


def test_lifecycle_records_complete_session_history(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("h1", "History task")["ok"]
    took = svc.take("h1")
    assert took["ok"], took
    gen = took["data"]["claim"]["generation"]
    assert svc.renew("h1", gen)["ok"]
    assert svc.release("h1", gen)["ok"]
    retook = svc.take("h1")
    assert retook["ok"], retook
    assert svc.finish("h1", retook["data"]["claim"]["generation"])["ok"]
    log = _sessions_of(ref, tmp_path / "cache")["h1"]["sessions"]
    assert [entry["op"] for entry in log] == ["create", "take", "renew", "release", "take", "finish"]
    assert {entry["op_id"] for entry in log} and len({entry["op_id"] for entry in log}) == 6
    for entry in log:
        assert entry["actor"] == "w1" and entry["session_id"] == "session-a"
        assert entry["client"] == "pytest" and "at" in entry
    assert "generation" not in log[0]  # create carries none
    assert log[1]["generation"] == gen  # take binds the claim it created
    assert log[4]["generation"] != gen  # re-adoption rotates the generation
    trail = git_backend.history(ref, limit=10)
    assert any(
        entry["session"] == "session-a" and entry["actor"] == "w1" for entry in trail
    ), trail


def test_sessionless_service_mutates_without_history(tmp_path: Path) -> None:
    svc = _svc(tmp_path, name="legacy.git")
    assert svc.create_item("l1", "Legacy task")["ok"]
    took = svc.take("l1")
    assert took["ok"], took
    assert svc.finish("l1", took["data"]["claim"]["generation"])["ok"]


def test_update_appends_session_entry(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("u1", "Updatable")["ok"]
    assert svc.update_item("u1", title="Updatable v2")["ok"]
    log = _sessions_of(ref, tmp_path / "cache")["u1"]["sessions"]
    assert [entry["op"] for entry in log] == ["create", "update"]


def test_noop_update_keeps_legacy_refusal(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("n1", "Noop")["ok"]
    out = svc.update_item("n1")
    assert not out["ok"] and out["code"] == "E_STATE"
    log = _sessions_of(ref, tmp_path / "cache")["n1"]["sessions"]
    assert [entry["op"] for entry in log] == ["create"]


def test_drop_records_session_entry(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("d1", "Droppable")["ok"]
    took = svc.take("d1")
    assert took["ok"], took
    assert svc.drop("d1", "no longer needed", took["data"]["claim"]["generation"])["ok"]
    log = _sessions_of(ref, tmp_path / "cache")["d1"]["sessions"]
    assert [entry["op"] for entry in log] == ["create", "take", "drop"]


def test_large_session_log_pages_within_cap(tmp_path: Path) -> None:
    from tractable import store as S

    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("big", "Big history")["ok"]
    # Seed 61 near-maximal MCP-shaped entries in ONE commit: 61 pushes
    # would take minutes, and the read path is what's under proof.
    actor = "mcp:claude-code:" + "u" * 32 + ":" + "a" * 24

    def seed(snap) -> dict:
        for i in range(61):
            assert S.record_session(
                snap, "big", actor=actor, session_id="s" * 256, client="c" * 64,
                op="renew", op_id=f"{i:032x}", generation=f"{9 - (i % 10):032x}",
            )
        return {"id": "big"}

    outcome = git_backend.mutate(
        ref, op="update", summary="seed history", worker="seeder", apply=seed,
    )
    assert outcome.ok, outcome
    seen: list[str] = []
    page = svc.show_item("big", field="sessions")
    assert page["ok"], page
    assert page["data"]["total"] == 62
    for _ in range(10):
        assert all("generation" not in entry for entry in page["data"]["window"])
        seen.extend(entry["op_id"] for entry in page["data"]["window"])
        cont = page["data"].get("continuation")
        if cont is None:
            break
        page = svc.show_item(
            "big", field="sessions", offset=cont["offset"], budget=cont["budget"], rev=cont["rev"],
        )
        assert page["ok"], page
    else:
        raise AssertionError("session pages did not terminate")
    assert len(seen) == 62 and len(set(seen)) == 62


def test_show_item_surfaces_session_summary_and_pages(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("s1", "Shown", description="body")["ok"]
    took = svc.take("s1")
    assert took["ok"], took
    shown = svc.show_item("s1")
    assert shown["ok"], shown
    summary = shown["data"]["sessions"]
    assert summary["total"] == 2
    assert summary["last"]["op"] == "take"
    assert summary["last"]["session_id"] == "session-a"
    # Generations authorize the holder's own take response; the read path
    # must not republish them, least of all the live one.
    assert "generation" not in summary["last"]
    assert summary["last"]["op_id"]
    assert "sessions_continuation" in shown["data"]
    assert "sessions" in shown["data"]["sections"]
    page = svc.show_item("s1", field="sessions")
    assert page["ok"], page
    assert page["data"]["total"] == 2
    assert [entry["op"] for entry in page["data"]["window"]] == ["create", "take"]
    assert "continuation" not in page["data"]
    rev = page["data"]["rev"]
    second = svc.show_item("s1", field="sessions", offset=1, rev=rev)
    assert second["ok"] and [entry["op"] for entry in second["data"]["window"]] == ["take"]
    stale = svc.show_item("s1", field="sessions", offset=1)
    assert not stale["ok"] and stale["code"] == "E_CURSOR_STALE"
    past_end = svc.show_item("s1", field="sessions", offset=9, rev=rev)
    assert not past_end["ok"]


def test_show_item_without_history_has_no_sessions_keys(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("plain", "Plain task")["ok"]
    shown = svc.show_item("plain")
    assert shown["ok"], shown
    assert "sessions" not in shown["data"]
    assert "sessions_continuation" not in shown["data"]


def _takeover_remote(tmp_path: Path, name: str = "takeover.git"):
    remote = tmp_path / name
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    return ref


def _takeover_svc(ref, cache: Path, worker: str, session: str | None) -> TrackerService:
    return TrackerService(ref=ref, cache_dir=cache, worker=worker, session_id=session)


def test_takeover_end_to_end_with_audit(tmp_path: Path) -> None:
    ref = _takeover_remote(tmp_path, name="t1.git")
    holder = _takeover_svc(ref, tmp_path / "cache-holder", "victim", "session-dead")
    assert holder.create_item("x1", "Takeable")["ok"]
    took = holder.take("x1")
    assert took["ok"], took
    old_gen = took["data"]["claim"]["generation"]
    taker = _takeover_svc(ref, tmp_path / "cache-taker", "taker", "session-a")
    out = taker.takeover("x1", "victim", "holder session exhausted")
    assert out["ok"], out
    data = out["data"]
    assert data["displaced"] == "victim"
    assert data["claim"]["generation"] != old_gen
    assert "transferred_batches" not in data
    log = _sessions_of(ref, tmp_path / "cache-taker")["x1"]["sessions"]
    assert [entry["op"] for entry in log] == ["create", "take", "takeover"]
    taken = log[-1]
    assert taken["actor"] == "taker" and taken["displaced"] == "victim"
    assert taken["reason"] == "holder session exhausted"
    # The displaced generation is dead.
    stale = holder.release("x1", old_gen)
    assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE"
    assert taker.renew("x1", data["claim"]["generation"])["ok"]


def test_takeover_refuses_without_session_bad_holder_and_owned_claims(tmp_path: Path) -> None:
    ref = _takeover_remote(tmp_path, name="t2.git")
    holder = _takeover_svc(ref, tmp_path / "cache-holder", "victim", "session-dead")
    assert holder.create_item("x1", "Takeable")["ok"]
    assert holder.take("x1")["ok"]
    nosession = _takeover_svc(ref, tmp_path / "cache-none", "taker", None)
    refused = nosession.takeover("x1", "victim", "dead session")
    assert not refused["ok"]
    taker = _takeover_svc(ref, tmp_path / "cache-taker", "taker", "session-a")
    wrong = taker.takeover("x1", "someone-else", "dead session")
    assert not wrong["ok"] and wrong["code"] == "E_CONFLICT"
    assert taker.create_item("mine", "Mine")["ok"]
    mine = taker.take("mine")
    assert mine["ok"], mine
    busy = taker.takeover("x1", "victim", "dead session")
    assert not busy["ok"] and busy["code"] == "E_MULTIPLE_CLAIMS"
    assert any("'mine'" in step for step in busy["recovery"])
    assert taker.release("mine", mine["data"]["claim"]["generation"])["ok"]
    assert taker.takeover("x1", "victim", "dead session")["ok"]


def test_takeover_retry_conflicts_on_changed_claim(tmp_path: Path, monkeypatch) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("t", "Task")["ok"]
    took = svc.take("t")
    assert took["ok"], took
    holder_gen = took["data"]["claim"]["generation"]

    def fake_mutate(ref, *, op, summary, worker, op_id, apply, max_retries=5, session=None, cache_dir=None, replay=None):
        first = git_backend.read(ref, tmp_path / "c1").snapshot
        apply(first)
        # The holder renews between our attempts: same worker, new image.
        second = git_backend.read(ref, tmp_path / "c2").snapshot
        S.op_renew(second, "t", "w1", holder_gen)
        try:
            apply(second)
        except TodoError as exc:
            return git_backend.MutationOutcome(
                ok=False, outcome="conflict", op_id=op_id,
                code=exc.code or "E_STATE", error=str(exc),
            )
        raise AssertionError("retry should have conflicted")

    monkeypatch.setattr(git_backend, "mutate", fake_mutate)
    taker = TrackerService(
        ref=ref, cache_dir=tmp_path / "cache-taker", worker="taker", session_id="session-a",
    )
    out = taker.takeover("t", "w1", "dead session")
    assert not out["ok"] and out["code"] == "E_CONFLICT"


def test_lifecycle_acks_are_compact(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    created = svc.create_item("t1", "First task", description="hello")
    assert created["ok"], created
    assert _size(created) <= 100 * 8  # well under the cap; ~100-token target
    took = svc.take("t1")
    assert took["ok"], took
    gen = took["data"]["claim"]["generation"]
    assert took["data"]["description_excerpt"] == "hello"
    finished = svc.finish("t1", gen)
    assert finished["ok"] and finished["data"]["status"] == "done"
    tokens, tokenizer = count_tokens(json.dumps(finished))
    assert tokenizer in ("o200k_base", "chars/4-fallback")
    assert tokens <= 100


def test_five_row_list_target(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(7):
        assert svc.create_item(f"t{i:02d}", f"Task {i}").get("ok")
    page = svc.list_items()
    assert page["ok"], page
    assert len(page["data"]["items"]) == 5
    assert page["data"]["total"] == 7
    assert "next_cursor" in page["data"]
    tokens, _ = count_tokens(json.dumps(page))
    assert tokens <= 600  # ~300-token target with headroom for IDs
    second = svc.list_items(cursor=page["data"]["next_cursor"])
    assert second["ok"] and len(second["data"]["items"]) == 2
    assert "next_cursor" not in second["data"]


def test_stale_cursor_never_skips_silently(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(6):
        svc.create_item(f"t{i:02d}", f"Task {i}")
    page = svc.list_items()
    svc.create_item("t-new", "Newcomer")
    stale = svc.list_items(cursor=page["data"]["next_cursor"])
    assert not stale["ok"] and stale["code"] == "E_CURSOR_STALE"


def test_cursor_bound_to_filters(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(6):
        svc.create_item(f"t{i:02d}", f"Task {i}")
    page = svc.list_items()
    assert page["ok"]
    # Same cursor with different filters restarts instead of skipping.
    reused = svc.list_items(cursor=page["data"]["next_cursor"], text="t00")
    assert not reused["ok"] and reused["code"] == "E_CURSOR_STALE"


def test_empty_ready_queue_reports_gate(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.create_item("t1", "Task one")
    empty = svc.list_items(ready_only=True)
    assert empty["ok"]  # claimable work exists
    took = svc.take("t1")
    assert took["ok"]
    idle = svc.list_items(ready_only=True)
    assert not idle["ok"] and idle["code"] == "E_NOTHING_READY"


def test_update_retry_conflicts_on_touched_fields(tmp_path: Path, monkeypatch) -> None:
    from tractable import store as S
    from tractable.errors import TodoError

    svc = _svc(tmp_path)
    assert svc.create_item("t", "v1")["ok"]

    def fake_mutate(ref, *, op, summary, worker, op_id, apply, max_retries=5, session=None, cache_dir=None, replay=None):
        # First attempt records the pre-image; a competing writer changes
        # the same field before the retry is evaluated.
        first = git_backend.read(ref, tmp_path / "c1").snapshot
        apply(first)
        second = git_backend.read(ref, tmp_path / "c2").snapshot
        S.op_update(second, "t", title="winner")
        try:
            apply(second)
        except TodoError as exc:
            return git_backend.MutationOutcome(
                ok=False, outcome="conflict", op_id=op_id,
                code=exc.code or "E_STATE", error=str(exc),
            )
        raise AssertionError("retry should have conflicted")

    monkeypatch.setattr(git_backend, "mutate", fake_mutate)
    out = svc.update_item("t", title="loser")
    assert not out["ok"] and out["code"] == "E_CONFLICT"


def test_take_with_huge_needs_keeps_generation(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    dep_ids = [f"d{i:03d}" for i in range(500)]

    def seed(snapshot: object) -> dict[str, str]:
        for dep_id in dep_ids:
            S.op_create(snapshot, item_id=dep_id, title=f"dep {dep_id}")
        return {"id": "seed"}

    assert git_backend.mutate(
        svc.ref, op="update", summary="seed huge-needs fixture",
        worker="seeder", apply=seed,
    ).ok
    assert svc.create_item("hub", "Hub", needs=[f"d{i:03d}" for i in range(500)])["ok"]
    took = svc.take("hub")
    assert took["ok"], took
    assert took["data"]["claim"]["generation"]
    assert _size(took) <= MAX_BYTES
    shown = svc.show_item("hub")
    assert shown["ok"] and _size(shown) <= MAX_BYTES
    assert shown["data"]["needs"]["total"] == 500
    first = svc.show_item("hub", field="needs", offset=0, budget=6000)
    assert first["ok"] and first["data"]["window"]
    assert "continuation" in first["data"]
    cont = first["data"]["continuation"]
    second = svc.show_item(
        "hub", field="needs", offset=cont["offset"], budget=6000, rev=cont["rev"])
    assert second["ok"]
    stale_section = svc.show_item(
        "hub", field="needs", offset=0, budget=6000, rev="0" * 40)
    assert not stale_section["ok"] and stale_section["code"] == "E_CURSOR_STALE"


def test_boundary_take_never_strands_generation(tmp_path: Path, monkeypatch) -> None:
    import tractable.service as svcmod

    svc = _svc(tmp_path)
    assert svc.create_item("edge", "Edge task", description="D" * 5000)["ok"]
    # Shrink the cap so the ack lands exactly on the boundary: the claim
    # must still come back with its generation.
    monkeypatch.setattr(svcmod, "MAX_BYTES", 3000)
    took = svc.take("edge")
    assert took["ok"], took
    assert took["data"]["claim"]["generation"]


def test_section_offset_requires_revision(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("s1", "Sectioned", description="E" * 20000)["ok"]
    first = svc.show_item("s1", field="description", offset=0, budget=6000)
    assert first["ok"]
    cont = first["data"]["continuation"]
    assert cont["rev"]
    # Omitting the revision on a continuation page fails closed.
    assert not svc.show_item("s1", field="description", offset=cont["offset"])["ok"]


def test_error_envelopes_stay_bounded(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    huge = "z" * 100000
    failed = svc.create_item("ok-id", huge)
    assert not failed["ok"] and _size(failed) <= MAX_BYTES
    missing = svc.show_item("nope", field="description")
    assert not missing["ok"] and _size(missing) <= MAX_BYTES


def test_oversized_item_reports_alternate_read(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    big = "x" * 60000 + "日本語" * 1000
    assert svc.create_item("big", "Big task", description=big)["ok"]
    shown = svc.show_item("big")
    assert shown["ok"], shown
    assert _size(shown) <= MAX_BYTES
    assert "description_continuation" in shown["data"]
    first = svc.show_item("big", field="description", offset=0, budget=6000)
    assert first["ok"] and first["data"]["window"]
    assert "continuation" in first["data"]
    cont = first["data"]["continuation"]
    second = svc.show_item(
        "big", field="description", offset=cont["offset"], budget=cont["budget"], rev=cont["rev"])
    assert second["ok"]
    past_end = svc.show_item("big", field="description", offset=10**9)
    assert not past_end["ok"]
    missing = svc.show_item("big", field="nope")
    assert not missing["ok"]


def test_many_deps_and_multibyte_stay_bounded(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(30):
        svc.create_item(f"d{i:02d}", f"dep task {i} héllo")
    svc.create_item("hub", "Hub task", needs=[f"d{i:02d}" for i in range(30)])
    shown = svc.show_item("hub")
    assert shown["ok"] and _size(shown) <= MAX_BYTES
    page = svc.list_items(limit=50)
    assert page["ok"] and _size(page) <= MAX_BYTES
    assert page["data"]["total"] == 31


def test_one_claim_per_worker(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.create_item("a1", "Task A1")
    svc.create_item("a2", "Task A2")
    assert svc.take("a1")["ok"]
    second = svc.take("a2")
    assert not second["ok"] and second["code"] == "E_MULTIPLE_CLAIMS"


def test_offline_read_uses_cache_but_mutation_fails(tmp_path: Path) -> None:
    remote = tmp_path / "gone.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1")
    assert svc.create_item("t1", "Task one")["ok"]
    assert svc.list_items()["ok"]  # populate the offline cache while online
    subprocess.run(["rm", "-rf", str(remote)], check=True)
    page = svc.list_items()
    assert page["ok"] and page["data"].get("stale") is True
    failed = svc.create_item("t2", "Task two")
    assert not failed["ok"] and failed["code"] == "E_OFFLINE"


def test_empty_page_with_remaining_items_is_unacceptable(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(10):
        svc.create_item(f"p{i:02d}", f"Task {i}")
    offset = 0
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = svc.list_items(limit=3, cursor=cursor) if cursor else svc.list_items(limit=3)
        assert page["ok"], page
        assert page["data"]["items"], "empty page with items remaining"
        seen.extend(item["id"] for item in page["data"]["items"])
        cursor = page["data"].get("next_cursor")
        if cursor is None:
            break
    assert len(seen) == 10
    assert offset == 0


def test_multiple_claims_refusal_tells_a_sibling_agent_what_to_do(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("held", "Held")["ok"]
    assert svc.create_item("other", "Other")["ok"]
    held = svc.take("held")
    assert held["ok"], held

    refused = svc.take("other")

    assert not refused["ok"]
    assert refused["code"] == "E_MULTIPLE_CLAIMS"
    assert refused["kind"] == "gate"
    assert "'held'" in refused["error"]
    steps = " ".join(refused["recovery"])
    assert "'held'" in steps
    assert "--actor" in steps
    assert svc.release("held", held["data"]["claim"]["generation"])["ok"]
    assert svc.take("other")["ok"]


def _branch_commits(ref, tmp_path: Path) -> int:
    out = subprocess.run(
        ["git", "--git-dir", ref.remote, "rev-list", "--count", ref.branch],
        check=True, capture_output=True, text=True,
    )
    return int(out.stdout.strip())


def test_drop_requires_reason_and_leaves_state_untouched(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("d1", "Droppable")["ok"]
    commits = _branch_commits(ref, tmp_path)
    for bad in ("", "   ", "x" * 2001, "bad\x00", "a\u0085b", "a\u2028b", "a\u2029b"):
        out = svc.drop("d1", bad)
        assert not out["ok"] and out["code"] == "E_STATE", (bad, out)
    assert _branch_commits(ref, tmp_path) == commits
    shown = svc.show_item("d1")
    assert shown["data"]["status"] == "open" and "drop_reason" not in shown["data"]


def test_drop_with_reason_is_one_state_commit(tmp_path: Path) -> None:
    svc, ref = _session_svc(tmp_path)
    assert svc.create_item("d1", "Droppable")["ok"]
    before = _branch_commits(ref, tmp_path)
    reason = "Superseded by d2.\n\tSee the follow-up."
    out = svc.drop("d1", "  " + reason + "\n")
    assert out["ok"] and out["data"]["status"] == "dropped", out
    assert _branch_commits(ref, tmp_path) == before + 1
    snap = git_backend.read(ref, tmp_path / "cache").snapshot
    assert snap.index["items"]["d1"]["status"] == "dropped"
    assert snap.details["d1"]["drop_reason"] == reason
    listed = svc.list_items(status="dropped")
    assert "drop_reason" not in json.dumps(listed)


def test_drop_retry_after_competing_push_stores_reason_once(tmp_path: Path, monkeypatch) -> None:
    svc, ref = _session_svc(tmp_path)
    rival = TrackerService(ref=ref, cache_dir=tmp_path / "rival-cache", worker="rival", session_id="session-b")
    assert svc.create_item("d1", "Droppable")["ok"]
    before = _branch_commits(ref, tmp_path)
    real_mutate = git_backend.mutate
    attempts: list[int] = []

    def racing_mutate(ref, *, apply, **kwargs):
        if kwargs.get("op") != "drop":
            return real_mutate(ref, apply=apply, **kwargs)

        def wrapped(snapshot):
            attempts.append(1)
            if len(attempts) == 1:
                # A competing writer lands between our read and our push.
                assert rival.create_item("other", "Other")["ok"]
            return apply(snapshot)

        return real_mutate(ref, apply=wrapped, **kwargs)

    monkeypatch.setattr(git_backend, "mutate", racing_mutate)
    out = svc.drop("d1", "obsolete")
    assert out["ok"], out
    assert len(attempts) == 2
    assert _branch_commits(ref, tmp_path) == before + 2  # the rival's commit and one drop commit
    snap = git_backend.read(ref, tmp_path / "cache").snapshot
    assert snap.details["d1"]["drop_reason"] == "obsolete"
    assert [entry["op"] for entry in snap.details["d1"]["sessions"]].count("drop") == 1


def test_show_item_returns_drop_reason_including_slim_fallback(tmp_path: Path, monkeypatch) -> None:
    import tractable.service as svcmod

    svc = _svc(tmp_path)
    assert svc.create_item("d1", "Droppable", description="D" * 5000)["ok"]
    assert svc.create_item("live", "Live")["ok"]
    reason = "Because.\nMultiple lines."
    assert svc.drop("d1", reason)["ok"]
    shown = svc.show_item("d1")
    assert shown["ok"] and shown["data"]["drop_reason"] == reason
    assert "drop_reason" not in svc.show_item("live")["data"]
    monkeypatch.setattr(svcmod, "MAX_BYTES", 3000)
    slim = svc.show_item("d1")
    assert slim["ok"] and "continuation" in slim["data"], slim
    assert slim["data"]["drop_reason"] == reason
