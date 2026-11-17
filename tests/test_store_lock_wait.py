"""A store held by maintenance fails a call quickly with nothing changed."""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

from tractable import git_backend, git_store
from tractable.git_backend import StateRef, bootstrap
from tractable.service import TrackerService


def _ref(tmp_path: Path) -> StateRef:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = StateRef(remote=str(remote), branch="todo-state")
    bootstrap(ref)
    return ref


def _hold_exclusive(cache: Path, ref: StateRef) -> int:
    ns = git_backend._ns_dir(cache, ref)
    ns.mkdir(parents=True, exist_ok=True)
    fd = os.open(ns / git_store.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _commits(ref: StateRef) -> int:
    return int(subprocess.run(
        ["git", "-C", ref.remote, "rev-list", "--count", ref.branch], check=True, capture_output=True, text=True,
    ).stdout)


def test_a_write_behind_maintenance_fails_unchanged_and_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _ref(tmp_path)
    cache = tmp_path / "cache"
    svc = TrackerService(ref=ref, cache_dir=cache, worker="w1")
    assert svc.create_item("t1", "One")["ok"]
    before = _commits(ref)
    monkeypatch.setattr(git_store, "LOCK_WAIT_S", 0.2)
    fd = _hold_exclusive(cache, ref)
    try:
        busy = svc.take("t1")
        assert not busy["ok"] and busy["code"] == "E_STATE"
        assert "nothing was changed" in busy["error"]
        assert "retry the call" in busy["recovery"]
        assert _commits(ref) == before
    finally:
        os.close(fd)
    assert svc.take("t1")["ok"]
    assert _commits(ref) == before + 1


def test_a_read_behind_maintenance_fails_with_a_coded_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _ref(tmp_path)
    cache = tmp_path / "cache"
    svc = TrackerService(ref=ref, cache_dir=cache, worker="w1")
    assert svc.create_item("t1", "One")["ok"]
    monkeypatch.setattr(git_store, "LOCK_WAIT_S", 0.2)
    fd = _hold_exclusive(cache, ref)
    try:
        busy = svc.list_items()
        assert not busy["ok"] and busy["code"] == "E_STATE" and "nothing was changed" in busy["error"]
    finally:
        os.close(fd)
    assert svc.list_items()["ok"]


def test_a_lock_released_in_time_is_waited_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = _ref(tmp_path)
    cache = tmp_path / "cache"
    svc = TrackerService(ref=ref, cache_dir=cache, worker="w1")
    assert svc.create_item("t1", "One")["ok"]
    fd = _hold_exclusive(cache, ref)
    slept: list[float] = []
    real_sleep = git_store.time.sleep

    def release_on_first_wait(delay: float) -> None:
        slept.append(delay)
        if len(slept) == 1:
            os.close(fd)
        real_sleep(0)

    monkeypatch.setattr(git_store.time, "sleep", release_on_first_wait)
    assert svc.take("t1")["ok"]
    assert slept
