"""Git publication proving cases against disposable local bare remotes."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from tractable import git_backend
from tractable import store as S
from tractable.errors import TodoError


def _remote(tmp_path: Path) -> git_backend.StateRef:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    return git_backend.StateRef(remote=str(remote), branch="todo-state")


def _tip(ref: git_backend.StateRef) -> str | None:
    return git_backend.ls_remote_tip(ref)


def test_bootstrap_refuses_to_overwrite() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ref = _remote(Path(d))
        sha = git_backend.bootstrap(ref)
        assert sha
        with pytest.raises(TodoError):
            git_backend.bootstrap(ref)


def test_round_trip_mutate_and_read(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    out = git_backend.mutate(
        ref, op="create", summary="add t1", worker="w1",
        apply=lambda snap: (S.op_create(snap, item_id="t1", title="Task one"), {"id": "t1"}),
    )
    assert out.ok and out.sha, out
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.rev == out.sha and ro.stale is False
    assert ro.snapshot.index["items"]["t1"]["title"] == "Task one"


def test_history_surfaces_actor_and_session_trailers(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref, worker="w0", session="session-zero")
    out = git_backend.mutate(
        ref, op="create", summary="add t1", worker="w1", session="session-a",
        apply=lambda snap: (S.op_create(snap, item_id="t1", title="Task one"), {"id": "t1"}),
    )
    assert out.ok and out.sha, out
    git_backend.mutate(
        ref, op="create", summary="add t2", worker="w1",
        apply=lambda snap: (S.op_create(snap, item_id="t2", title="Task two"), {"id": "t2"}),
    )
    entries = {entry["sha"]: entry for entry in git_backend.history(ref, limit=10)}
    assert entries[out.sha]["actor"] == "w1"
    assert entries[out.sha]["session"] == "session-a"
    assert entries[out.sha]["op_id"]
    boot = [entry for entry in entries.values() if entry["subject"].startswith("todo(bootstrap)")]
    assert boot and boot[0]["actor"] == "w0" and boot[0]["session"] == "session-zero"
    unattributed = [entry for sha, entry in entries.items() if sha != out.sha and not entry["subject"].startswith("todo(bootstrap)")]
    assert unattributed and all(entry["session"] == "" for entry in unattributed)


def test_commit_trailers_first_match_wins_over_injected_lines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    body = (
        "todo(create): add t1\n\n"
        "Todo-Op-Id: " + "a" * 32 + "\n"
        "Todo-Actor: w1\n"
        "Todo-Session: s1\n"
        "Todo-Session: forged-session\n"
        "x\u2028Todo-Op-Id: " + "f" * 32 + "\n"
    )
    (repo / "body.txt").write_text(body)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "--allow-empty", "-F", str(repo / "body.txt")],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert git_backend._commit_trailers(repo, sha) == {
        "op_id": "a" * 32, "actor": "w1", "session": "s1",
    }


def test_missing_branch_read_reports_bootstrap(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    with pytest.raises(TodoError):
        git_backend.read(ref, tmp_path / "cache")


def test_race_for_one_task_has_one_winner(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add contested", worker="setup",
        apply=lambda snap: (S.op_create(snap, item_id="c", title="Contested"), {}),
    )
    results: dict[str, git_backend.MutationOutcome] = {}

    def take(worker: str) -> None:
        results[worker] = git_backend.mutate(
            ref, op="take", summary=f"{worker} takes c", worker=worker,
            apply=lambda snap: (S.op_take(snap, "c", worker), {}),
        )

    threads = [threading.Thread(target=take, args=(w,)) for w in ("aaa", "bbb")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    winners = [w for w, r in results.items() if r.ok]
    losers = [w for w, r in results.items() if not r.ok]
    assert len(winners) == 1 and len(losers) == 1
    loser = losers[0]
    assert results[loser].code in ("E_CONFLICT", "E_CLAIM_STALE")
    # Loser cannot release or finish the winner's task.
    winner = winners[0]
    ro = git_backend.read(ref, tmp_path / "cache")
    gen = ro.snapshot.index["items"]["c"]["claim"]["generation"]
    assert ro.snapshot.index["items"]["c"]["claim"]["worker"] == winner
    bad_release = git_backend.mutate(
        ref, op="release", summary="loser releases", worker=loser,
        apply=lambda snap: (S.op_release(snap, "c", loser, gen), {}),
    )
    assert not bad_release.ok
    bad_finish = git_backend.mutate(
        ref, op="finish", summary="loser finishes", worker=loser,
        apply=lambda snap: (S.op_finish(snap, "c", loser, gen), {}),
    )
    assert not bad_finish.ok


def test_unrelated_concurrent_ops_both_survive(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    results: dict[str, git_backend.MutationOutcome] = {}

    def create(item_id: str) -> None:
        results[item_id] = git_backend.mutate(
            ref, op="create", summary=f"add {item_id}", worker=item_id,
            apply=lambda snap: (S.op_create(snap, item_id=item_id, title=f"Task {item_id}"), {}),
        )

    threads = [threading.Thread(target=create, args=(i,)) for i in ("u1", "u2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(r.ok for r in results.values()), results
    ro = git_backend.read(ref, tmp_path / "cache2")
    assert set(ro.snapshot.index["items"]) == {"u1", "u2"}


def test_conflicting_edits_never_silently_overwrite(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add t", worker="setup",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {}),
    )

    def update_if_v1(snap: S.Snapshot) -> dict:
        if snap.index["items"]["t"]["title"] != "v1":
            raise TodoError("conflict: title is no longer v1", code="E_CONFLICT")
        return S.op_update(snap, "t", title="v2")

    def update_if_v1_other(snap: S.Snapshot) -> dict:
        if snap.index["items"]["t"]["title"] != "v1":
            raise TodoError("conflict: title is no longer v1", code="E_CONFLICT")
        return S.op_update(snap, "t", title="v3")

    results: dict[str, git_backend.MutationOutcome] = {}
    threads = [
        threading.Thread(target=lambda: results.setdefault("a", git_backend.mutate(
            ref, op="update", summary="set v2", worker="a", apply=update_if_v1))),
        threading.Thread(target=lambda: results.setdefault("b", git_backend.mutate(
            ref, op="update", summary="set v3", worker="b", apply=update_if_v1_other))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    oks = [k for k, r in results.items() if r.ok]
    assert len(oks) == 1
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.snapshot.index["items"]["t"]["title"] in ("v2", "v3")


def test_lost_reply_reconciles_without_duplicate(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    op_id = git_backend.new_op_id()

    def create(snap: S.Snapshot) -> dict:
        S.op_create(snap, item_id="once", title="Once")
        return {"id": "once"}

    first = git_backend.mutate(ref, op="create", summary="add once", worker="w", op_id=op_id, apply=create)
    assert first.ok
    before = _tip(ref)
    second = git_backend.mutate(ref, op="create", summary="add once", worker="w", op_id=op_id, apply=create)
    assert second.ok and second.sha == first.sha
    assert _tip(ref) == before
    found = git_backend.reconcile(ref, op_id)
    assert found.state == "applied" and found.sha == first.sha
    missing = git_backend.reconcile(ref, "0" * 32)
    assert missing.state == "absent" and missing.sha == before
    with pytest.raises(TodoError):
        git_backend.reconcile(ref, "not-an-op-id")


def test_crash_before_commit_leaves_tip_unchanged(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    before = _tip(ref)

    def boom(snap: S.Snapshot) -> dict:
        raise RuntimeError("simulated crash before commit")

    with pytest.raises(RuntimeError):
        git_backend.mutate(ref, op="create", summary="boom", worker="w", apply=boom)
    assert _tip(ref) == before


def test_offline_mutation_never_succeeds_locally(tmp_path: Path) -> None:
    ref = git_backend.StateRef(remote=str(tmp_path / "nope.git"), branch="todo-state")
    out = git_backend.mutate(
        ref, op="create", summary="offline", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="x", title="X"), {}),
    )
    assert not out.ok and out.outcome == "offline"


def test_caches_are_namespaced_per_state_branch(tmp_path: Path) -> None:
    ref_a = _remote(tmp_path)
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(other)], check=True)
    ref_b = git_backend.StateRef(remote=str(other), branch="todo-state")
    git_backend.bootstrap(ref_a)
    git_backend.bootstrap(ref_b)
    git_backend.mutate(
        ref_a, op="create", summary="add a", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="only-a", title="A"), {}),
    )
    shared = tmp_path / "shared-cache"
    ro_a = git_backend.read(ref_a, shared)
    assert "only-a" in ro_a.snapshot.index["items"]
    # B shares the cache directory but must never see A's state, online or off.
    ro_b = git_backend.read(ref_b, shared)
    assert "only-a" not in ro_b.snapshot.index["items"]
    subprocess.run(["rm", "-rf", str(other)], check=True)
    offline_b = git_backend.read(ref_b, shared)
    assert offline_b.stale is True
    assert "only-a" not in offline_b.snapshot.index["items"]


def test_read_revision_is_the_loaded_commit(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add t", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {}),
    )
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.rev == _tip(ref)
    # The reported revision always matches the loaded content.
    at_rev = git_backend.read_rev(ref, ro.rev)
    assert at_rev.index == ro.snapshot.index
    assert at_rev.details == ro.snapshot.details


def test_push_landed_but_confirmation_lost_is_unknown(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)

    def flaky_tip(r):
        raise TodoError("simulated confirmation blackout", code="E_OFFLINE")

    def blind_reconcile(work, r, op_id):
        return git_backend.ReconcileResult(state="unknown", detail="simulated blackout")

    monkeypatch.setattr(git_backend, "ls_remote_tip", flaky_tip)
    monkeypatch.setattr(git_backend, "_reconcile", blind_reconcile)
    out = git_backend.mutate(
        ref, op="create", summary="add ghost", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="ghost", title="Ghost"), {}),
    )
    # The push landed but every confirmation channel failed: unknown with
    # the operation ID, never a blind success and never a blind failure.
    assert not out.ok and out.outcome == "unknown" and out.code == "E_UNKNOWN"
    assert out.op_id in (out.error or "")
    monkeypatch.undo()
    found = git_backend.reconcile(ref, out.op_id)
    assert found.state == "applied"


def test_inline_trailer_mention_never_reconciles(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    # A commit whose message merely mentions the trailer inline (never as
    # its own line) must not reconcile as that operation.
    work = tmp_path / "mw"
    subprocess.run(["git", "clone", "--quiet", str(ref.remote), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "fetch", "--quiet", "origin", "todo-state"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "FETCH_HEAD"], check=True)
    (work / "note.txt").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "note.txt"], check=True)
    victim = "ab" * 16
    subprocess.run(
        ["git", "-C", str(work), "commit", "--quiet", "-m",
         f"todo(note): mentions Todo-Op-Id: {victim} inline"],
        check=True)
    subprocess.run(["git", "-C", str(work), "push", "--quiet", "origin", "HEAD:todo-state"], check=True)
    found = git_backend.reconcile(ref, victim)
    assert found.state == "absent"


def test_restore_refuses_off_branch_revisions(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    foreign = tmp_path / "foreign.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(foreign)], check=True)
    work = tmp_path / "fw"
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    (work / "f").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "--quiet", "-m", "foreign"], check=True)
    sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(work), "push", "--quiet", str(foreign), "HEAD:todo-state"], check=True)
    with pytest.raises(TodoError):
        git_backend.restore_rev(ref, sha, "w")


def test_readers_stay_on_one_accepted_snapshot(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add t", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {}),
    )
    ro = git_backend.read(ref, tmp_path / "cache")
    git_backend.mutate(
        ref, op="update", summary="set v2", worker="w",
        apply=lambda snap: S.op_update(snap, "t", title="v2"),
    )
    assert ro.snapshot.index["items"]["t"]["title"] == "v1"
    ro2 = git_backend.read(ref, tmp_path / "cache")
    assert ro2.snapshot.index["items"]["t"]["title"] == "v2"
    assert ro.rev != ro2.rev


def test_state_operations_never_clone_the_whole_remote(tmp_path: Path, monkeypatch) -> None:
    """Writes, reconcile, history and restore fetch only the state branch.

    A full clone pulls every ref of a remote that also carries code
    history; against production state that exceeded the git timeout and
    made item creation fail.
    """
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    real_git = git_backend._git

    def spy(args: list[str], cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] == "clone":
            raise AssertionError(f"state operations must not clone: git {args}")
        if args and args[0] == "fetch":
            # Name the one branch: a bare fetch (or --all) pulls every ref.
            assert args[-1].startswith(f"+refs/heads/{ref.branch}:refs/tractable/in/"), args
            assert "--all" not in args, args
        return real_git(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", spy)
    out = git_backend.mutate(
        ref, op="create", summary="add t", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {"id": "t"}),
    )
    assert out.ok, out.error
    assert git_backend.reconcile(ref, out.op_id).state == "applied"
    assert git_backend.history(ref, limit=5)[0]["op_id"] == out.op_id
    first = git_backend.history(ref, limit=5)[-1]["sha"]
    assert git_backend.restore_rev(ref, first, "w").ok


def _create(snap, item_id: str = "t"):
    S.op_create(snap, item_id=item_id, title="v1")
    return {"id": item_id}


def _fail_after_push(monkeypatch, *, failing: set[str]) -> None:
    """Make the named git subcommands time out once a push has been sent."""
    real_git = git_backend._git
    state = {"pushed": False}

    def spy(args: list[str], cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        proc = real_git(args, cwd, timeout, **kw)
        if args and args[0] == "push":
            state["pushed"] = True
        elif state["pushed"] and args and args[0] in failing:
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return proc

    monkeypatch.setattr(git_backend, "_git", spy)


def test_mutate_reports_fetch_timeout_as_offline(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    real_git = git_backend._git

    def spy(args: list[str], cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] == "fetch":
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real_git(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", spy)
    out = git_backend.mutate(ref, op="create", summary="add t", worker="w", apply=_create)
    assert not out.ok and out.outcome == "offline"
    assert out.op_id in (out.error or "")


def test_timeout_confirming_a_landed_push_still_reports_applied(tmp_path: Path, monkeypatch) -> None:
    """A confirm timeout after a successful push reconciles instead of raising."""
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    _fail_after_push(monkeypatch, failing={"ls-remote"})
    out = git_backend.mutate(ref, op="create", summary="add t", worker="w", apply=_create)
    assert out.ok and out.outcome == "applied", out.error


def test_timeout_during_reconcile_returns_unknown_with_operation_id(tmp_path: Path, monkeypatch) -> None:
    """When the push landed but nothing can confirm it, hand back the op ID."""
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    _fail_after_push(monkeypatch, failing={"ls-remote", "fetch"})
    out = git_backend.mutate(ref, op="create", summary="add t", worker="w", apply=_create)
    assert not out.ok and out.outcome == "unknown"
    assert out.op_id in (out.error or "")
    monkeypatch.undo()
    assert git_backend.reconcile(ref, out.op_id).state == "applied"


def test_missing_state_branch_points_at_bootstrap(tmp_path: Path) -> None:
    ref = _remote(tmp_path)  # bare remote, no state branch
    out = git_backend.mutate(ref, op="create", summary="add t", worker="w", apply=_create)
    assert not out.ok and out.code == "E_STATE" and "bootstrap" in (out.error or "")
    for call in (
        lambda: git_backend.history(ref),
        lambda: git_backend.read_rev(ref, "0" * 40),
        lambda: git_backend.restore_rev(ref, "0" * 40, "w"),
    ):
        with pytest.raises(TodoError) as exc:
            call()
        assert exc.value.code == "E_STATE" and "bootstrap" in str(exc.value)


def test_relative_local_remote_works_for_state_operations(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    monkeypatch.chdir(tmp_path)
    rel = git_backend.StateRef(remote="state.git", branch=ref.branch)
    out = git_backend.mutate(rel, op="create", summary="add t", worker="w", apply=_create)
    assert out.ok, out.error
    assert git_backend.reconcile(rel, out.op_id).state == "applied"
    assert git_backend.history(rel, limit=1)[0]["op_id"] == out.op_id


def test_read_never_clones_the_whole_remote(tmp_path: Path, monkeypatch) -> None:
    """Reads must fetch only the state branch, never clone all refs.

    Regression guard for the production slow read: remotes that also carry
    code history make a full clone dominate read latency, while reads only
    need the state branch tip.
    """
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    seen: list[list[str]] = []
    real_git = git_backend._git

    def spy(args: list[str], cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] == "clone":
            seen.append(list(args))
            raise AssertionError(f"read() must not clone: git {args}")
        return real_git(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", spy)
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.stale is False
    assert seen == []


def test_read_reuses_tip_snapshot_without_network(tmp_path: Path, monkeypatch) -> None:
    """A repeat read at the same tip serves the store without git transport."""
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add t", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {}),
    )
    cache = tmp_path / "cache"
    first = git_backend.read(ref, cache)
    assert first.stale is False

    # Same tip, dead transport past the tip check: the stored snapshot
    # must serve from local objects without any fetch.
    monkeypatch.setattr(git_backend, "ls_remote_tip", lambda r, timeout=0: first.rev)
    real_git = git_backend._git

    def no_network(args: list[str], cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] in {"fetch", "ls-remote", "push", "clone"}:
            raise AssertionError(f"stored-tip read must not use the network: {args}")
        return real_git(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", no_network)
    second = git_backend.read(ref, cache)
    assert second.rev == first.rev and second.stale is False
    assert second.snapshot.index == first.snapshot.index
    assert second.snapshot.details == first.snapshot.details
