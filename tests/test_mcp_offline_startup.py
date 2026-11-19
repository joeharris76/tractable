"""Startup and reads with an unreachable state remote."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from tractable import git_backend
from tractable.errors import TodoError
from tractable.mcp import server as mcp_server
from tractable.mcp.target import ResolvedTarget
from tractable.service import SECTION_BUDGET, TrackerService

from test_mcp_stdio import _call_tool, _initialize, _make_state, _spawn_server


def _setup(tmp_path: Path) -> tuple[TrackerService, ResolvedTarget, Path]:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    svc = TrackerService(ref=ref, cache_dir=cache, worker="w1")
    target = ResolvedTarget(state_ref=ref, cache_dir=cache, source="flag", config_path=None)
    return svc, target, remote


def _populate(svc: TrackerService) -> None:
    assert svc.create_item("t1", "Task one", description="d" * (SECTION_BUDGET * 3))["ok"]
    assert svc.defer_item("t1", "Later", "needs infra")["ok"]
    assert svc.list_items()["ok"]


def _away(remote: Path) -> Path:
    moved = remote.with_name("away.git")
    remote.rename(moved)
    return moved


def test_startup_succeeds_offline_with_cache_and_serves_stale(tmp_path: Path, caplog) -> None:
    svc, target, remote = _setup(tmp_path)
    _populate(svc)
    cached = svc.list_items()["data"]["rev"]
    _away(remote)
    with caplog.at_level(logging.WARNING, logger="tractable.mcp"):
        mcp_server.startup_check(target)
    assert cached in caplog.text

    listed = svc.list_items()["data"]
    assert listed["stale"] is True and listed["cached_rev"] == cached and "rev" not in listed
    shown = svc.show_item("t1")["data"]
    assert shown["stale"] is True and shown["cached_rev"] == cached and "rev" not in shown
    cont = shown["description_continuation"]
    assert cont["rev"] == cached
    section = svc.show_item("t1", field="description", offset=cont["offset"], budget=cont["budget"], rev=cont["rev"])
    assert section["ok"], section
    assert section["data"]["stale"] is True and section["data"]["cached_rev"] == cached
    assert "rev" not in section["data"] and section["data"]["continuation"]["rev"] == cached
    needs = svc.show_item("t1", field="needs")["data"]
    assert needs["stale"] is True and needs["cached_rev"] == cached
    deferrals = svc.list_deferrals()["data"]
    assert deferrals["stale"] is True and deferrals["cached_rev"] == cached


def test_started_offline_mutation_fails_then_reads_recover(tmp_path: Path) -> None:
    svc, target, remote = _setup(tmp_path)
    _populate(svc)
    moved = _away(remote)
    mcp_server.startup_check(target)
    failed = svc.create_item("t2", "Task two")
    assert not failed["ok"] and failed["code"] == "E_OFFLINE"
    assert any("reachable" in step for step in failed["recovery"])
    moved.rename(remote)
    fresh = svc.list_items()["data"]
    assert "stale" not in fresh and "cached_rev" not in fresh and fresh["rev"]
    assert "stale" not in svc.show_item("t1")["data"]
    assert svc.create_item("t2", "Task two")["ok"]


def _hanging_probe(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    real = git_backend._git

    def fake(args, cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kwargs):
        if args and args[0] == "ls-remote":
            seen.append(timeout)
            raise subprocess.TimeoutExpired(args, timeout)
        return real(args, cwd, timeout, **kwargs)

    monkeypatch.setattr(git_backend, "_git", fake)
    return seen


def test_cached_startup_probe_is_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, target, _ = _setup(tmp_path)
    _populate(svc)
    seen = _hanging_probe(monkeypatch)
    mcp_server.startup_check(target)
    assert seen == [mcp_server.STARTUP_PROBE_TIMEOUT_S]
    assert mcp_server.STARTUP_PROBE_TIMEOUT_S <= 10 < git_backend.GIT_TIMEOUT_S


def test_startup_reads_only_the_cached_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, target, remote = _setup(tmp_path)
    _populate(svc)

    def refuse(*args, **kwargs):
        raise AssertionError("startup loaded the cached snapshot")

    monkeypatch.setattr(git_backend.Store, "load", refuse)
    monkeypatch.setattr(git_backend, "load_snapshot", refuse)
    mcp_server.startup_check(target)
    _away(remote)
    mcp_server.startup_check(target)


def test_no_cache_unreachable_fails_with_full_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, target, remote = _setup(tmp_path)
    seen = _hanging_probe(monkeypatch)
    with pytest.raises(TodoError, match="state remote unreachable") as info:
        mcp_server.startup_check(target)
    assert info.value.code == "E_OFFLINE"
    assert seen == [git_backend.GIT_TIMEOUT_S]
    monkeypatch.undo()
    _away(remote)
    with pytest.raises(TodoError, match="state remote unreachable"):
        mcp_server.startup_check(target)


@pytest.mark.parametrize("cached", [False, True])
def test_missing_branch_fails_with_bootstrap_hint(tmp_path: Path, cached: bool) -> None:
    svc, target, remote = _setup(tmp_path)
    if cached:
        _populate(svc)
    subprocess.run(["git", "-C", str(remote), "branch", "-D", "todo-state"], check=True, capture_output=True)
    with pytest.raises(TodoError, match="run `tractable bootstrap` first") as info:
        mcp_server.startup_check(target)
    assert info.value.code == "E_STATE"


def test_stdio_server_starts_offline_from_cache(tmp_path: Path) -> None:
    remote = Path(_make_state(tmp_path))
    cache = tmp_path / "cache"

    def session(steps):
        proc = _spawn_server(str(remote), cache)
        try:
            _initialize(proc)
            return steps(proc)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    created = session(lambda p: _call_tool(p, "create_item", {"id": "t1", "title": "Task one"}, 2))
    assert created["ok"], created
    moved = _away(remote)

    def offline(proc):
        listed = _call_tool(proc, "list_items", {}, 2)
        shown = _call_tool(proc, "show_item", {"id": "t1"}, 3)
        failed = _call_tool(proc, "create_item", {"id": "t2", "title": "Task two"}, 4)
        moved.rename(remote)
        fresh = _call_tool(proc, "list_items", {}, 5)
        return listed, shown, failed, fresh

    listed, shown, failed, fresh = session(offline)
    assert listed["data"]["stale"] is True and listed["data"]["cached_rev"]
    assert shown["data"]["stale"] is True and shown["data"]["cached_rev"]
    assert failed["ok"] is False and failed["code"] == "E_OFFLINE" and failed["recovery"]
    assert "stale" not in fresh["data"] and fresh["data"]["rev"]
    json.dumps(fresh)


def test_show_item_from_cache_is_marked_stale(tmp_path: Path) -> None:
    svc, _, remote = _setup(tmp_path)
    _populate(svc)
    cached = svc.list_items()["data"]["rev"]
    assert svc.show_item("t1")["data"]["rev"] == cached
    _away(remote)
    shown = svc.show_item("t1")["data"]
    assert shown["stale"] is True and shown["cached_rev"] == cached and "rev" not in shown


def test_cached_read_against_a_hanging_remote_probes_briefly_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _, _ = _setup(tmp_path)
    _populate(svc)
    seen = _hanging_probe(monkeypatch)
    out = svc.list_items()
    assert out["ok"] and out["data"].get("stale") is True
    assert seen == [git_backend.READ_PROBE_TIMEOUT_S]
    assert git_backend.READ_PROBE_TIMEOUT_S < git_backend.GIT_TIMEOUT_S
    monkeypatch.undo()
    assert "stale" not in svc.list_items()["data"]


def test_read_without_a_cache_keeps_the_full_probe_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _, _ = _setup(tmp_path)
    seen = _hanging_probe(monkeypatch)
    out = svc.list_items()
    assert not out["ok"]
    assert seen == [git_backend.GIT_TIMEOUT_S]
