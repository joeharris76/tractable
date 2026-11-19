"""The persistent Git object store behind reads, writes and recovery."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tractable import git_backend
from tractable import git_store
from tractable import store as S
from tractable.errors import TodoError

WORDS = (
    "state branch item claim worker session needs ready blocked open done review "
    "plan test commit push fetch tree index detail cache store snapshot deferral "
    "batch member prepared evidence scope generation priority medium high low"
).split()


def _git(cwd: Path, *args: str, **kw) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def _remote(tmp_path: Path) -> git_backend.StateRef:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    return git_backend.StateRef(remote=str(remote), branch="todo-state")


def _prose(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def _objects_bytes(store: git_store.Store) -> int:
    root = store.git_dir / "objects"
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


Files = dict[str, "str | tuple[str, str]"]


def _worktree_commit(tmp_path: Path, files: Files, *, mode_exec: frozenset[str] = frozenset()) -> tuple[Path, str]:
    """Commit raw files on a fresh branch repo; ("link", target) makes a symlink."""
    work = tmp_path / "src"
    subprocess.run(["git", "init", "--quiet", "-b", "todo-state", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    for rel, body in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, tuple):
            path.symlink_to(body[1])
        else:
            path.write_text(body, encoding="utf-8")
        if rel in mode_exec:
            path.chmod(0o755)
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "fixture")
    return work, _git(work, "rev-parse", "HEAD")


def _fetched_store(tmp_path: Path, url: Path, branch: str = "todo-state") -> tuple[git_store.Store, str]:
    store = git_store.Store(tmp_path / "ns", git_store.run_git)
    store.open()
    with store.incoming(str(url), branch) as (proc, tip):
        assert proc.returncode == 0, proc.stderr
        store.advance_tip(tip)
    return store, tip


def _index(*ids: str) -> tuple[str, dict[str, str]]:
    snap = S.Snapshot(index=S.empty_index(), details={})
    for item_id in ids:
        S.op_create(snap, item_id=item_id, title=item_id)
    return S.serialize_state(snap.index), {i: S.serialize_state(snap.details[i]) for i in ids}


def _valid_files(*ids: str) -> Files:
    index, details = _index(*ids)
    return {"index.json": index, **{f"items/{i}.json": body for i, body in details.items()}}


def _parity_cases() -> dict[str, Files]:
    base = _valid_files("a", "b")
    return {
        "valid": base,
        "empty-items": {"index.json": _index()[0]},
        "missing-detail": {k: v for k, v in base.items() if k != "items/b.json"},
        "extra-detail": {**base, "items/zzz.json": json.dumps({"id": "zzz"})},
        "dot-file": {**base, "items/.hidden.json": "{}"},
        "nested-ignored": {**base, "items/sub/deep.json": "not json"},
        "non-json-ignored": {**base, "items/notes.txt": "hello"},
        "symlinked-index": {**base, "real.json": base["index.json"], "index.json": ("link", "real.json")},
        "symlinked-detail": {**base, "real.json": base["items/b.json"], "items/b.json": ("link", "../real.json")},
        "invalid-json": {**base, "items/a.json": "{not json"},
        "missing-index": {k: v for k, v in base.items() if k != "index.json"},
    }


@pytest.mark.parametrize("case", sorted(_parity_cases()))
def test_store_loader_matches_checkout_loader(tmp_path: Path, case: str) -> None:
    files = _parity_cases()[case]
    work, sha = _worktree_commit(tmp_path, files)
    store, tip = _fetched_store(tmp_path, work)
    assert tip == sha

    def outcome(load):
        try:
            snap = load()
            return ("ok", snap.index, snap.details)
        except TodoError as exc:
            return ("error", exc.code)

    assert outcome(lambda: store.load(sha)[0]) == outcome(lambda: S.load_snapshot(work))


def test_store_loader_refuses_symlinked_items_directory(tmp_path: Path) -> None:
    index, details = _index("a")
    work = tmp_path / "src"
    subprocess.run(["git", "init", "--quiet", "-b", "todo-state", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    (work / "index.json").write_text(index)
    (work / "real").mkdir()
    (work / "real" / "a.json").write_text(details["a"])
    (work / "items").symlink_to("real")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "fixture")
    store, tip = _fetched_store(tmp_path, work)
    with pytest.raises(TodoError, match="symlinked items directory"):
        store.load(tip)
    with pytest.raises(TodoError, match="symlinked items directory"):
        S.load_snapshot(work)


def _porcelain_commit(tmp_path: Path, url: Path, apply, message: str) -> tuple[str, str]:
    """The pre-store write path: checkout, rewrite every file, git add, git commit -m."""
    work = tmp_path / "porcelain"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "tractable")
    _git(work, "config", "user.email", "tractable@localhost")
    _git(work, "fetch", "--quiet", str(url), "todo-state")
    _git(work, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    snap = S.load_snapshot(work)
    apply(snap)
    S.save_snapshot(work, snap)
    _git(work, "add", "--", "index.json", "items")
    _git(work, "commit", "--quiet", "-m", message)
    body = subprocess.run(["git", "cat-file", "commit", "HEAD"], cwd=work, check=True,
                          capture_output=True, text=True).stdout
    return _git(work, "rev-parse", "HEAD^{tree}"), body.split("\n\n", 1)[1]


def test_plumbing_commit_matches_porcelain_commit(tmp_path: Path) -> None:
    files = _valid_files("a", "b", "c")
    # A non-canonical blob and an executable bit: the porcelain path
    # normalizes both by rewriting every file, so the store must too.
    files["items/c.json"] = json.dumps(json.loads(files["items/c.json"]))
    work, sha = _worktree_commit(tmp_path, files, mode_exec={"items/a.json"})

    def apply(snap: S.Snapshot) -> None:
        S.op_update(snap, "a", title="renamed")
        del snap.index["items"]["b"]
        del snap.details["b"]
        S.op_create(snap, item_id="d", title="new one")

    message = git_backend._commit_message(
        "update", "line one  \n\n\nline two\t", "a" * 32, "worker", "session-1",
    )
    want_tree, want_body = _porcelain_commit(tmp_path, work, apply, message)

    store, tip = _fetched_store(tmp_path, work)
    snap, current = store.load(tip)
    apply(snap)
    tree = store.write_tree(tip, snap, current)
    commit = store.commit(tree, tip, message)
    body = store.git(["cat-file", "commit", commit]).stdout.split("\n\n", 1)[1]
    assert tree == want_tree
    assert body == want_body
    assert "Todo-Op-Id: " + "a" * 32 in body


def test_cleanup_message_matches_git_stripspace() -> None:
    samples = ["", "\n\n", "a", "a\n", "  a  \n\n\n\nb \t\n\n", "\nx\n\n\ny\n\n", "t\r\nu\r\n"]
    for sample in samples:
        want = subprocess.run(["git", "stripspace"], input=sample, capture_output=True, text=True, check=True).stdout
        assert git_store.cleanup_message(sample) == want, repr(sample)


def test_tip_never_moves_backwards_but_follows_a_rewrite(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    first = git_backend.mutate(ref, op="create", summary="a", worker="w",
                               apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    second = git_backend.mutate(ref, op="create", summary="b", worker="w",
                                apply=lambda s: (S.op_create(s, item_id="b", title="B"), {})[1])
    store = git_backend._store(ref, None)
    assert store.tip() == second.sha
    store.advance_tip(first.sha)
    assert store.tip() == second.sha
    # A rewritten branch (not a descendant) still replaces the tip.
    orphan = store._ok(["commit-tree", store.tree_of(first.sha)], input="rewrite\n",
                       env=git_store.COMMIT_IDENTITY).strip()
    store.advance_tip(orphan)
    assert store.tip() == orphan


def test_read_follows_a_force_pushed_branch(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    base = git_backend.ls_remote_tip(ref)
    git_backend.mutate(ref, op="create", summary="a", worker="w",
                       apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    cache = tmp_path / "cache"
    assert "a" in git_backend.read(ref, cache).snapshot.index["items"]
    newer = git_backend._store(ref, cache).tip()
    # A rewind looks exactly like a slow process reporting an older tip, so
    # the stored tip keeps the newer commit; the read still serves the remote.
    subprocess.run(["git", "-C", ref.remote, "update-ref", "refs/heads/todo-state", base], check=True)
    ro = git_backend.read(ref, cache)
    assert ro.rev == base and "a" not in ro.snapshot.index["items"]
    assert git_backend._store(ref, cache).tip() == newer
    # A divergent rewrite is followed: the remote is authoritative.
    work = tmp_path / "rewrite"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    _git(work, "fetch", "--quiet", ref.remote, "todo-state")
    _git(work, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    snap = S.load_snapshot(work)
    S.op_create(snap, item_id="b", title="B")
    S.save_snapshot(work, snap)
    _git(work, "add", "--", "index.json", "items")
    _git(work, "commit", "--quiet", "-m", "divergent")
    _git(work, "push", "--quiet", "--force", ref.remote, "HEAD:todo-state")
    ro = git_backend.read(ref, cache)
    assert "b" in ro.snapshot.index["items"] and "a" not in ro.snapshot.index["items"]
    assert git_backend._store(ref, cache).tip() == ro.rev


def test_size_per_interaction_is_within_twice_the_items_touched(tmp_path: Path) -> None:
    """Retained store growth per one-item commit, after maintenance, is <= 2x that item."""
    rng = random.Random(7)
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    ids = [f"item-{n:03d}" for n in range(300)]

    def seed(snap: S.Snapshot) -> dict:
        for item_id in ids:
            S.op_create(snap, item_id=item_id, title=_prose(rng, 8)[:200], description=_prose(rng, 500))
        return {}

    assert git_backend.mutate(ref, op="seed", summary="seed", worker="w", apply=seed, cache_dir=cache).ok
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    store.maintain()
    before = _objects_bytes(store)

    touched = 0
    for n in range(50):
        item_id = ids[rng.randrange(len(ids))]
        sentence = _prose(rng, 12)

        def edit(snap: S.Snapshot, item_id=item_id, sentence=sentence) -> dict:
            detail = snap.details[item_id]
            S.op_update(snap, item_id, description=detail.get("description", "") + " " + sentence)
            return {}

        out = git_backend.mutate(ref, op="update", summary=f"edit {n}", worker="w", apply=edit, cache_dir=cache)
        assert out.ok, out.error
        ro = git_backend.read(ref, cache)
        touched += len(S.serialize_state(ro.snapshot.details[item_id]).encode())
        assert store.slack() <= git_store.SLACK_CAP_BYTES
    store.maintain()
    growth = _objects_bytes(store) - before
    print(f"store growth {growth} B for {touched} B of items touched ({growth / touched:.2f}x)")
    assert growth <= 2 * touched, (growth, touched)


def test_maintenance_starts_once_slack_passes_the_cap(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    calls: list[int] = []
    monkeypatch.setattr(git_store.Store, "maintain", lambda self: calls.append(1) or True)
    monkeypatch.setattr(git_store, "SLACK_CAP_BYTES", 10**12)
    store.maybe_maintain()
    assert calls == []
    monkeypatch.setattr(git_store, "SLACK_CAP_BYTES", -1)
    store.maybe_maintain()
    assert calls == [1]


def test_maintenance_is_skipped_while_another_process_runs_it(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    import fcntl

    fd = os.open(store.ns / git_store.MAINTENANCE_LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert store.maintain() is False
    finally:
        os.close(fd)
    assert store.maintain() is True


def test_damaged_store_is_rebuilt_on_read(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(ref, op="create", summary="a", worker="w",
                       apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    old_id = store.store_id()
    for pack in (store.git_dir / "objects").rglob("*"):
        if pack.is_file() and pack.parent.name != "info":
            pack.unlink()
    ro = git_backend.read(ref, cache)
    # Missing objects are simply fetched again.
    assert "a" in ro.snapshot.index["items"] and ro.stale is False

    # A store that fails locally is moved aside and rebuilt once.
    real_load = git_store.Store.load
    calls = {"n": 0}

    def failing_once(self, rev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise git_store.StoreError("simulated damage")
        return real_load(self, rev)

    monkeypatch.setattr(git_store.Store, "load", failing_once)
    ro = git_backend.read(ref, cache)
    assert "a" in ro.snapshot.index["items"] and calls["n"] == 2
    assert store.store_id() != old_id


def test_offline_read_serves_the_store_tip(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    out = git_backend.mutate(ref, op="create", summary="a", worker="w",
                             apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    git_backend._store(ref, cache).maintain()
    subprocess.run(["rm", "-rf", ref.remote], check=True)
    ro = git_backend.read(ref, cache)
    assert ro.stale is True and ro.rev == out.sha and "a" in ro.snapshot.index["items"]


def test_offline_read_falls_back_to_a_legacy_cached_revision(tmp_path: Path, monkeypatch) -> None:
    ref = git_backend.StateRef(remote="https://example.invalid/state.git", branch="todo-state")
    cache = tmp_path / "cache"
    legacy = git_backend._ns_dir(cache, ref)
    snap = S.Snapshot(index=S.empty_index(), details={})
    S.op_create(snap, item_id="legacy", title="Legacy")
    S.save_snapshot(legacy / "revs" / ("c" * 40), snap)
    (legacy / "last").write_text("c" * 40 + "\n")

    def offline(r, timeout=0):
        raise TodoError("simulated outage", code="E_OFFLINE")

    monkeypatch.setattr(git_backend, "ls_remote_tip", offline)
    ro = git_backend.read(ref, cache)
    assert ro.stale is True and ro.rev == "c" * 40 and "legacy" in ro.snapshot.index["items"]


def test_reads_never_write_snapshot_files(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    assert not list(cache.rglob("revs")) and not list(cache.rglob("last"))


def test_inherited_git_environment_and_global_hooks_are_ignored(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "pre-push", "post-commit"):
        hook = hooks / name
        # Fail only inside the cache: with a local-path remote the same
        # global hooks also run in the remote's receive-pack.
        hook.write_text('#!/bin/sh\ncase "$(pwd)" in *default-cache*) exit 1;; esac\nexit 0\n')
        hook.chmod(0o755)
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgSign = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere-index"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    out = git_backend.mutate(ref, op="create", summary="a", worker="w",
                             apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    assert out.ok, out.error
    author = subprocess.run(["git", "-C", ref.remote, "log", "-1", "--format=%an <%ae>", "todo-state"],
                            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                            capture_output=True, text=True, check=True).stdout.strip()
    assert author == "tractable <tractable@localhost>"


def test_local_relative_remotes_get_their_own_store(tmp_path: Path, monkeypatch) -> None:
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        subprocess.run(["git", "init", "--quiet", "--bare", str(tmp_path / name / "state.git")], check=True)
    rel = git_backend.StateRef(remote="state.git", branch="todo-state")
    monkeypatch.chdir(tmp_path / "one")
    one = git_backend._store(rel, tmp_path / "cache").ns
    monkeypatch.chdir(tmp_path / "two")
    two = git_backend._store(rel, tmp_path / "cache").ns
    assert one != two


WORKER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from tractable import git_backend, store as S
    remote, cache, name, count = sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    ref = git_backend.StateRef(remote=remote, branch="todo-state")
    errors, created = [], []
    for n in range(count):
        item_id = f"{name}-{n}"
        op_id = git_backend.new_op_id()
        try:
            # Retry exhaustion under heavy contention is a reported outcome;
            # resubmitting under the same operation ID must never duplicate.
            for _ in range(20):
                out = git_backend.mutate(
                    ref, op="create", summary=item_id, worker=name, cache_dir=cache, op_id=op_id,
                    apply=lambda s: (S.op_create(s, item_id=item_id, title=item_id), {})[1],
                )
                if out.ok or out.code != "E_CONFLICT":
                    break
            if out.ok:
                created.append(item_id)
            else:
                errors.append(out.error)
            git_backend.read(ref, cache)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    print(json.dumps({"errors": errors, "created": created}))
""")

MAINTAINER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from tractable import git_store
    ns, stop = Path(sys.argv[1]), Path(sys.argv[2])
    runs = 0
    while not stop.exists():
        if git_store.Store(ns, git_store.run_git).maintain():
            runs += 1
        time.sleep(0.05)
    print(runs)
""")


def test_concurrent_processes_share_one_store_safely(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    stop = tmp_path / "stop"
    env = {**os.environ, "TRACTABLE_STORE_MAINTENANCE": "off"}
    maintainer = subprocess.Popen([sys.executable, "-c", MAINTAINER, str(store.ns), str(stop)],
                                  stdout=subprocess.PIPE, text=True, env=env)
    workers = [
        subprocess.Popen([sys.executable, "-c", WORKER, ref.remote, str(cache), f"w{n}", "3"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for n in range(8)
    ]
    results = [w.communicate(timeout=600) for w in workers]
    stop.touch()
    runs = int(maintainer.communicate(timeout=120)[0].strip() or 0)
    created: set[str] = set()
    for out, err in results:
        report = json.loads(out)
        assert report["errors"] == [], err
        assert "cannot lock ref" not in err and "Traceback" not in err
        created |= set(report["created"])
    assert runs >= 1
    ro = git_backend.read(ref, cache)
    expected = {f"w{n}-{i}" for n in range(8) for i in range(3)}
    assert created == expected and expected <= set(ro.snapshot.index["items"])
    creates = subprocess.run(["git", "-C", ref.remote, "log", "--format=%s", "todo-state"],
                             capture_output=True, text=True, check=True).stdout.split("\n")
    assert sorted(line for line in creates if line.startswith("todo(create)")) == sorted(
        f"todo(create): {item}" for item in expected)
    assert store.tip() == git_backend.ls_remote_tip(ref)
    assert not list((store.git_dir / "refs" / "tractable" / "in").glob("*"))
    subprocess.run(["rm", "-rf", ref.remote], check=True)
    offline = git_backend.read(ref, cache)
    assert offline.stale is True and offline.rev == ro.rev


def test_cache_prune_removes_only_obsolete_entries(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    live = git_backend._store(ref, cache).ns
    for legacy in (live, cache / ("1" * 16)):
        (legacy / "revs" / ("a" * 40)).mkdir(parents=True)
        (legacy / "last").write_text("a" * 40 + "\n")
    (live / ".last.1.x.tmp").write_text("partial")
    (cache / ("b" * 40)).mkdir()
    (cache / ".trash-old").mkdir()
    os.utime(cache / ".trash-old", (1_000_000_000, 1_000_000_000))
    (cache / ".trash-fresh").mkdir()
    stale = cache / ("2" * 16)
    (stale / "revs").mkdir(parents=True)
    old = 1_000_000_000
    for path in (stale / "revs", stale):
        os.utime(path, (old, old))
    (cache / "not-a-namespace").mkdir()

    dry = git_store.prune_cache(cache, dry_run=True)
    assert (live / "revs").exists() and (cache / ("b" * 40)).exists()
    removed = {Path(p).relative_to(cache).as_posix() for p in dry["removed"]}
    assert removed == {
        "b" * 40, ".trash-old", "2" * 16,
        f"{live.name}/revs", f"{live.name}/last", f"{live.name}/.last.1.x.tmp",
    }

    report = git_store.prune_cache(cache)
    assert {Path(p).relative_to(cache).as_posix() for p in report["removed"]} == removed
    assert report["maintained"] == [str(live)]
    # The namespace without a store keeps its legacy revision: its only offline copy.
    assert (cache / ("1" * 16) / "revs").exists() and (cache / "not-a-namespace").exists()
    assert sorted(p.name for p in cache.iterdir()) == sorted([live.name, "1" * 16, "not-a-namespace", ".trash-fresh"])
    assert git_backend.read(ref, cache).stale is False


def test_failed_operation_search_never_reapplies(tmp_path: Path, monkeypatch) -> None:
    """A history search that cannot run is not absence: repair, then find the commit."""
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    op_id = git_backend.new_op_id()

    def create(snap: S.Snapshot) -> dict:
        S.op_create(snap, item_id="once", title="Once")
        return {}

    first = git_backend.mutate(ref, op="create", summary="once", worker="w", op_id=op_id, apply=create)
    assert first.ok
    real_git = git_backend._git
    failed = {"n": 0}

    def broken_log(args, cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] == "log" and failed["n"] == 0:
            failed["n"] += 1
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: missing parent")
        return real_git(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", broken_log)
    again = git_backend.mutate(ref, op="create", summary="once", worker="w", op_id=op_id, apply=create)
    assert again.ok and again.sha == first.sha and again.ack.get("duplicate") is True
    assert git_backend.ls_remote_tip(ref) == first.sha and failed["n"] == 1


def test_reconcile_reports_unknown_when_history_cannot_be_searched(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    real_git = git_backend._git

    def broken_log(args, cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] == "log":
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: bad object")
        return real_git(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", broken_log)
    assert git_backend.reconcile(ref, "0" * 32).state == "unknown"


def test_read_rev_ignores_nested_detail_files(tmp_path: Path) -> None:
    files = _valid_files("a")
    files["items/z/a.json"] = json.dumps({"description": "nested value"})
    work, sha = _worktree_commit(tmp_path, files)
    ref = git_backend.StateRef(remote=str(work), branch="todo-state")
    snap = git_backend.read_rev(ref, sha, cache_dir=tmp_path / "cache")
    assert snap.details["a"] == S.load_snapshot(work).details["a"]
    assert "nested value" not in json.dumps(snap.details)


def test_repair_leaves_a_usable_store_and_skips_a_newer_one(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    old = store.store_id()
    store.repair(old)
    assert store.exists() and store.store_id() not in ("", old)
    newer = store.store_id()
    store.repair(old)  # a delayed repair of the first store must not touch its replacement
    assert store.store_id() == newer
    with store.using():
        assert store.active_id == newer


def test_using_recreates_a_missing_store(tmp_path: Path) -> None:
    store = git_store.Store(tmp_path / "ns", git_store.run_git)
    with store.using():
        assert store.exists()


def test_cache_prune_skips_busy_namespaces_and_fresh_staging(tmp_path: Path) -> None:
    import fcntl

    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    live = git_backend._store(ref, cache).ns
    fresh, old_staging = live / ".init-fresh", live / ".init-old"
    fresh.mkdir()
    old_staging.mkdir()
    os.utime(old_staging, (1_000_000_000, 1_000_000_000))
    busy = cache / ("3" * 16)
    (busy / "revs").mkdir(parents=True)
    os.utime(busy / "revs", (1_000_000_000, 1_000_000_000))
    fd = os.open(busy / git_store.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        report = git_store.prune_cache(cache, older_than_days=0)
    finally:
        os.close(fd)
    assert str(busy) in report["skipped_busy"] and busy.exists()
    assert fresh.exists() and not old_staging.exists()
    # --older-than 0 is raised to the one-day minimum: the live namespace stays.
    assert live.exists() and git_backend.read(ref, cache).stale is False


def test_cache_prune_clears_a_local_remotes_legacy_namespace(tmp_path: Path, monkeypatch, capsys) -> None:
    from tractable.cli import main

    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    monkeypatch.chdir(tmp_path)
    rel = git_backend.StateRef(remote="state.git", branch="todo-state")
    legacy = git_backend._ns_dir(cache, rel)
    (legacy / "revs" / ("a" * 40)).mkdir(parents=True)
    git_backend.read(rel, cache)
    assert git_backend._store(rel, cache).ns != legacy
    assert main(["cache", "prune", "--cache-dir", str(cache), "--state-remote", "state.git"]) == 0
    capsys.readouterr()
    assert not (legacy / "revs").exists()


def test_inherited_command_level_config_cannot_reenable_hooks(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "pre-push"):
        hook = hooks / name
        hook.write_text('#!/bin/sh\ncase "$(pwd)" in *default-cache*) exit 1;; esac\nexit 0\n')
        hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hookspath'='{hooks}'")
    out = git_backend.mutate(ref, op="create", summary="a", worker="w",
                             apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    assert out.ok, out.error


def test_hard_slack_cap_ignores_the_cooldown(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    (store.ns / git_store.MAINTENANCE_STAMP_NAME).touch()
    calls: list[int] = []
    monkeypatch.setattr(git_store.Store, "maintain", lambda self: calls.append(1) or True)
    monkeypatch.setenv(git_store.MAINTENANCE_ENV, "background")
    monkeypatch.setattr(git_store.Store, "slack", lambda self: 100)
    monkeypatch.setattr(git_store, "SLACK_CAP_BYTES", 10)
    monkeypatch.setattr(git_store, "HARD_SLACK_CAP_BYTES", 1000)
    store.maybe_maintain()  # over the soft cap, inside the cooldown: nothing runs
    assert calls == []
    monkeypatch.setattr(git_store, "HARD_SLACK_CAP_BYTES", 50)
    store.maybe_maintain()  # over the hard cap: runs inline despite the cooldown
    assert calls == [1]


def test_failed_maintenance_is_recorded_and_cleared(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    real_ok = git_store.Store._ok

    def failing_repack(self, args, **kw):
        if args[0] == "repack":
            raise git_store.StoreError("simulated repack failure")
        return real_ok(self, args, **kw)

    monkeypatch.setattr(git_store.Store, "_ok", failing_repack)
    with pytest.raises(git_store.StoreError):
        store.maintain()
    assert "simulated repack failure" in (store.ns / git_store.MAINTENANCE_ERROR_NAME).read_text()
    monkeypatch.undo()
    assert store.maintain() is True
    assert not (store.ns / git_store.MAINTENANCE_ERROR_NAME).exists()


def test_background_maintenance_runs_in_a_detached_process(tmp_path: Path, monkeypatch) -> None:
    import time

    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    for n in range(3):
        git_backend.mutate(ref, op="create", summary=f"t{n}", worker="w", cache_dir=cache,
                           apply=lambda s, n=n: (S.op_create(s, item_id=f"t{n}", title="T"), {})[1])
        git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    objects = store.git_dir / "objects"

    def loose() -> int:
        return sum(1 for d in objects.iterdir() if len(d.name) == 2 for _ in d.iterdir())

    assert loose() > 0
    monkeypatch.setenv(git_store.MAINTENANCE_ENV, "background")
    monkeypatch.setattr(git_store, "SLACK_CAP_BYTES", 0)
    store.maybe_maintain()
    deadline = time.time() + 60
    while time.time() < deadline and loose() > 0:
        time.sleep(0.2)
    assert loose() == 0 and (store.ns / git_store.MAINTENANCE_STAMP_NAME).exists()
    assert not (store.ns / git_store.MAINTENANCE_ERROR_NAME).exists()


def test_small_edits_cost_a_bounded_fixed_overhead_per_commit(tmp_path: Path) -> None:
    """Title-only edits touch ~140 bytes; git's per-commit record dominates.

    The bound for small items is 2x the bytes touched (detail plus index
    entry) plus FIXED_COMMIT_OVERHEAD per commit, not 2x alone.
    """
    fixed_commit_overhead = 1024
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"

    def seed(snap: S.Snapshot) -> dict:
        for n in range(300):
            S.op_create(snap, item_id=f"i{n:03d}", title=f"task {n}")
        return {}

    assert git_backend.mutate(ref, op="seed", summary="seed", worker="w", apply=seed, cache_dir=cache).ok
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    store.maintain()
    before = _objects_bytes(store)
    rng = random.Random(5)
    touched, commits = 0, 30
    for k in range(commits):
        item_id = f"i{rng.randrange(300):03d}"
        out = git_backend.mutate(ref, op="update", summary=f"t{k}", worker="w", cache_dir=cache,
                                 apply=lambda s, i=item_id, k=k: S.op_update(s, i, title=f"task {i} rev {k}"))
        assert out.ok, out.error
        snap = git_backend.read(ref, cache).snapshot
        touched += len(S.serialize_state(snap.details[item_id]).encode())
        touched += len(S.serialize_state({item_id: snap.index["items"][item_id]}).encode())
    store.maintain()
    growth = _objects_bytes(store) - before
    print(f"small edits: {growth / commits:.0f} B per commit for {touched / commits:.0f} B touched")
    assert growth <= 2 * touched + fixed_commit_overhead * commits, (growth, touched)


def test_git_never_reads_the_callers_stdin() -> None:
    """Under the MCP server stdin is the JSON-RPC stream; git must not consume it."""
    out = git_store.run_git(["hash-object", "--stdin"], timeout=10)
    assert out.returncode == 0 and out.stdout.strip() == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_slack_ignores_pack_indexes(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    store.maintain()
    assert list((store.git_dir / "objects" / "pack").glob("*.idx"))
    assert store.slack() == 0


@pytest.mark.parametrize("rev", ["-n1", "HEAD", "abc", "g" * 40])
def test_read_rev_rejects_anything_but_a_full_commit_id(tmp_path: Path, monkeypatch, rev: str) -> None:
    ref = _remote(tmp_path)
    monkeypatch.setattr(git_backend, "_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError(a)))
    with pytest.raises(git_backend.RevisionNotFound):
        git_backend.read_rev(ref, rev, cache_dir=tmp_path / "cache")


def test_dry_run_reports_busy_namespaces_without_creating_locks(tmp_path: Path) -> None:
    import fcntl

    cache = tmp_path / "cache"
    busy, idle = cache / ("4" * 16), cache / ("5" * 16)
    for ns in (busy, idle):
        (ns / "revs").mkdir(parents=True)
    fd = os.open(busy / git_store.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        report = git_store.prune_cache(cache, dry_run=True)
    finally:
        os.close(fd)
    assert report["skipped_busy"] == [str(busy)]
    assert not (idle / git_store.LOCK_NAME).exists()


def test_store_creation_refuses_git_below_the_minimum(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(git_store, "git_version", lambda: (2, 30, 9))
    store = git_store.Store(tmp_path / "ns", git_store.run_git)
    with pytest.raises(git_store.StoreError, match=r"git 2\.31 or newer is required; found 2\.30\.9"):
        store.open()
    assert git_store.MIN_GIT_VERSION == (2, 31)
