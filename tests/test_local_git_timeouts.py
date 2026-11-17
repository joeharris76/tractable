"""A local git command that times out becomes a coded error, never a raw exception."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from tractable import git_backend
from tractable.git_backend import StateRef, bootstrap
from tractable.git_store import Store
from tractable.service import TrackerService

LOCAL_COMMANDS = (
    "cat-file",
    "commit-tree",
    "hash-object",
    "log",
    "ls-tree",
    "merge-base",
    "read-tree",
    "update-index",
    "update-ref",
    "write-tree",
)
CODED = {"E_STATE", "E_UNKNOWN", "E_OFFLINE"}


def _service(tmp_path: Path) -> TrackerService:
    remote = tmp_path / "state.git"
    if not remote.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
        bootstrap(StateRef(remote=str(remote), branch="todo-state"))
    ref = StateRef(remote=str(remote), branch="todo-state")
    return TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1")


def _time_out_once(monkeypatch: pytest.MonkeyPatch, command: str) -> dict[str, int]:
    original = git_backend._git
    calls = {"n": 0}

    def injected(args, cwd=None, timeout=60, **kwargs):
        if args[0] == command and calls["n"] == 0:
            calls["n"] += 1
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return original(args, cwd, timeout, **kwargs)

    monkeypatch.setattr(git_backend, "_git", injected)
    return calls


def _never_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    def rebuilt(store) -> None:
        raise AssertionError("a timeout must not rebuild the local store")

    monkeypatch.setattr(git_backend, "_repair", rebuilt)


READS = ("list_items", "show_item")
APPLICABLE = [(operation, command) for operation in READS for command in ("cat-file", "ls-tree")] + [
    ("create_item", command) for command in LOCAL_COMMANDS
]


@pytest.mark.parametrize(("operation", "command"), APPLICABLE)
def test_a_local_git_timeout_returns_a_coded_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, command: str,
) -> None:
    service = _service(tmp_path)
    assert service.create_item("seed", "Seed")["ok"]
    actions = {
        "list_items": lambda: service.list_items(),
        "show_item": lambda: service.show_item("seed"),
        "create_item": lambda: dataclasses.replace(service, operation_id="e" * 32).create_item("created", "Created"),
    }
    calls = _time_out_once(monkeypatch, command)
    _never_rebuild(monkeypatch)

    result = actions[operation]()

    assert calls["n"] == 1, f"{operation} did not run git {command}"
    assert isinstance(result, dict) and "ok" in result, result
    if not result["ok"]:
        assert result["code"] in CODED, result
        assert "timed out" in result["error"], result


def test_a_timeout_before_the_push_says_nothing_was_pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    calls = _time_out_once(monkeypatch, "commit-tree")
    _never_rebuild(monkeypatch)

    result = service.create_item("created", "Created")

    assert calls["n"] == 1
    assert not result["ok"]
    assert result["code"] == "E_STATE"
    assert "before anything was pushed" in result["error"]
    assert result["recovery"] == ["retry the call"]
    monkeypatch.undo()
    assert not _service(tmp_path).show_item("created")["ok"]


def test_a_timeout_after_the_push_started_is_reported_as_unknown_and_the_write_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    original = Store.advance_tip
    calls = {"n": 0}

    def advance_then_time_out(self, new):
        calls["n"] += 1
        if calls["n"] == 2:
            raise subprocess.TimeoutExpired(["git", "update-ref"], 60)
        return original(self, new)

    monkeypatch.setattr(Store, "advance_tip", advance_then_time_out)

    result = service.create_item("created", "Created")

    assert calls["n"] == 2
    assert not result["ok"]
    assert result["code"] == "E_UNKNOWN"
    assert "after the push started" in result["error"]
    assert result["recovery"]
    monkeypatch.undo()
    assert _service(tmp_path).show_item("created")["ok"]
