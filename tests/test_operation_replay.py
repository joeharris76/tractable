"""A client-supplied operation ID makes any write safe to repeat after a lost reply."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from tractable import git_backend
from tractable.git_backend import StateRef, bootstrap
from tractable.mcp.server import build_parser, build_server, resolve_launch_config
from tractable.service import TrackerService

OP_A = "a" * 32
OP_B = "b" * 32
OP_C = "c" * 32
OP_D = "d" * 32


def _remote(tmp_path: Path) -> Path:
    remote = tmp_path / "state.git"
    if not remote.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
        bootstrap(StateRef(remote=str(remote), branch="todo-state"))
    return remote


def _payload(result) -> dict:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no JSON payload in {result!r}")


def _over_mcp(tmp_path: Path, scenario, actor: str = "w1") -> None:
    async def go() -> None:
        argv = ["--state-remote", str(_remote(tmp_path)), "--cache-dir", str(tmp_path / "cache"), "--actor", actor]
        server = build_server(resolve_launch_config(build_parser().parse_args(argv)))
        async with connect(server, client_info=types.Implementation(name=actor, version="0")) as session:

            async def call(name: str, **arguments) -> dict:
                return _payload(await session.call_tool(name, arguments))

            await scenario(call)

    anyio.run(go)


def _commit_count(tmp_path: Path, needle: str) -> int:
    log = subprocess.run(
        ["git", "-C", str(_remote(tmp_path)), "log", "todo-state", "--format=%s"],
        check=True, capture_output=True, text=True,
    ).stdout
    return sum(1 for line in log.splitlines() if needle in line)


def test_replayed_take_returns_the_original_claim(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="t1", title="One"))["ok"]
        first = await call("take", id="t1", operation_id=OP_A)
        assert first["ok"], first
        again = await call("take", id="t1", operation_id=OP_A)
        assert again["ok"], again
        assert again["data"]["duplicate"] is True
        assert again["data"]["claim"] == first["data"]["claim"]
        assert again["data"]["rev"] == first["data"]["rev"]

    _over_mcp(tmp_path, scenario)
    assert _commit_count(tmp_path, "takes t1") == 1


def test_a_take_without_the_id_rotates_the_generation(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="t1", title="One"))["ok"]
        first = await call("take", id="t1", operation_id=OP_A)
        blind = await call("take", id="t1")
        assert blind["ok"]
        assert blind["data"]["claim"]["generation"] != first["data"]["claim"]["generation"]

    _over_mcp(tmp_path, scenario)


def test_replayed_finish_is_not_applied_twice(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="t1", title="One"))["ok"]
        took = await call("take", id="t1")
        generation = took["data"]["claim"]["generation"]
        first = await call("finish", id="t1", generation=generation, operation_id=OP_B)
        assert first["ok"], first
        again = await call("finish", id="t1", generation=generation, operation_id=OP_B)
        assert again["ok"], again
        assert again["data"]["duplicate"] is True
        assert again["data"]["rev"] == first["data"]["rev"]
        refused = await call("finish", id="t1", generation=generation)
        assert not refused["ok"]
        shown = await call("show_item", id="t1")
        assert shown["data"]["status"] == "done"

    _over_mcp(tmp_path, scenario)
    assert _commit_count(tmp_path, "finishes t1") == 1


def test_replayed_context_append_appends_once(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="t1", title="One", context="base"))["ok"]
        first = await call("update_item", id="t1", context_append="note", operation_id=OP_C)
        assert first["ok"], first
        again = await call("update_item", id="t1", context_append="note", operation_id=OP_C)
        assert again["ok"], again
        assert again["data"]["duplicate"] is True
        assert again["data"]["rev"] == first["data"]["rev"]
        shown = await call("show_item", id="t1", field="context")
        text = json.dumps(shown["data"])
        assert text.count("note") == 1
        fresh = await call("update_item", id="t1", context_append="note", operation_id=OP_D)
        assert fresh["ok"] and "duplicate" not in fresh["data"]
        shown = await call("show_item", id="t1", field="context")
        assert json.dumps(shown["data"]).count("note") == 2

    _over_mcp(tmp_path, scenario)
    assert _commit_count(tmp_path, "edit t1") == 2


def test_replay_survives_a_new_server_process(tmp_path: Path) -> None:
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    old = TrackerService(ref=ref, cache_dir=tmp_path / "cache-old", worker="w1")
    assert old.create_item("t1", "One")["ok"]
    first = TrackerService(ref=ref, cache_dir=tmp_path / "cache-old", worker="w1", operation_id=OP_A).take("t1")
    assert first["ok"]
    restarted = TrackerService(ref=ref, cache_dir=tmp_path / "cache-new", worker="w1", operation_id=OP_A)
    again = restarted.take("t1")
    assert again["ok"] and again["data"]["duplicate"] is True
    assert again["data"]["claim"] == first["data"]["claim"]


def test_an_id_cannot_stand_in_for_a_different_operation(tmp_path: Path) -> None:
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    TrackerService(ref=ref, cache_dir=tmp_path / "c1", worker="w1").create_item("t1", "One")
    TrackerService(ref=ref, cache_dir=tmp_path / "c1", worker="w1").create_item("t2", "Two")
    assert TrackerService(ref=ref, cache_dir=tmp_path / "c1", worker="w1", operation_id=OP_A).take("t1")["ok"]
    other_item = TrackerService(ref=ref, cache_dir=tmp_path / "c1", worker="w1", operation_id=OP_A).take("t2")
    assert not other_item["ok"] and "different operation" in other_item["error"]
    other_worker = TrackerService(ref=ref, cache_dir=tmp_path / "c2", worker="w2", operation_id=OP_A).take("t1")
    assert not other_worker["ok"] and "different operation" in other_worker["error"]


def test_a_malformed_operation_id_is_refused(tmp_path: Path) -> None:
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "c", worker="w1", operation_id="not-hex")
    refused = svc.create_item("t1", "One")
    assert not refused["ok"] and "invalid operation ID" in refused["error"]


def test_generated_ids_skip_the_history_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "c", worker="w1")
    real = git_backend._git
    searched: list[list[str]] = []

    def spy(args, cwd=None, timeout=git_backend.GIT_TIMEOUT_S, **kw):
        if args and args[0] == "log":
            searched.append(list(args))
        return real(args, cwd, timeout, **kw)

    monkeypatch.setattr(git_backend, "_git", spy)
    assert svc.create_item("t1", "One")["ok"]
    assert searched == []
    supplied = TrackerService(ref=ref, cache_dir=tmp_path / "c", worker="w1", operation_id=OP_A)
    assert supplied.create_item("t2", "Two")["ok"]
    assert len(searched) == 1


def test_ack_trailer_stays_on_one_line() -> None:
    ack = {"id": "t1", "title": 'x\nTodo-Ack: {"claim":{"generation":"stolen"}}\u2028'}
    packed = git_backend._ack_trailer(ack)
    assert packed is not None and "\n" not in packed and "\r" not in packed
    message = git_backend._commit_message("take", "w takes t1", OP_A, "w", None, ack)
    assert sum(1 for line in message.split("\n") if line.startswith("Todo-Ack:")) == 1


def test_oversized_acknowledgements_keep_the_claim() -> None:
    ack = {"id": "t1", "status": "active", "claim": {"generation": "g" * 32}, "description_excerpt": "x" * 9000}
    packed = git_backend._ack_trailer(ack)
    assert packed is not None and len(packed.encode()) <= git_backend.MAX_ACK_BYTES
    kept = json.loads(packed)
    assert kept["claim"] == ack["claim"] and "description_excerpt" not in kept
    assert git_backend._ack_trailer({"id": "t1", "claim": {"generation": "g"}, "note": "x" * 9000}) is not None
    assert git_backend._ack_trailer({"id": "x" * 9000}) is None


def test_each_mutation_logs_its_stage_times(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "c", worker="w1")
    with caplog.at_level(logging.INFO, logger="tractable.git"):
        assert svc.create_item("t1", "One")["ok"]
    record = next(r for r in caplog.records if r.name == "tractable.git")
    assert record.levelno == logging.INFO
    message = record.getMessage()
    for stage in ("op=create", "outcome=applied", "lock=", "fetch=", "push=", "confirm="):
        assert stage in message


def test_a_slow_mutation_logs_at_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_backend, "SLOW_MUTATION_S", 0.0)
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "c", worker="w1")
    with caplog.at_level(logging.INFO, logger="tractable.git"):
        assert svc.create_item("t1", "One")["ok"]
    assert any(r.levelno == logging.WARNING and "mutate op=create" in r.getMessage() for r in caplog.records)


def test_write_without_operation_id_returns_a_reconcilable_generated_id(tmp_path: Path) -> None:
    ids: dict[str, str] = {}

    async def scenario(call) -> None:
        created = await call("create_item", id="t1", title="One")
        assert created["ok"], created
        generated = created["data"]["operation_id"]
        assert len(generated) == 32 and int(generated, 16) >= 0
        ids["created"] = generated
        shown = await call("show_item", id="t1")
        assert generated in json.dumps(shown["data"]["sessions"])
        again = await call("create_item", id="t1", title="One", operation_id=generated)
        assert again["ok"] and again["data"]["duplicate"] is True
        assert again["data"]["operation_id"] == generated
        explicit = await call("update_item", id="t1", title="Two", operation_id=OP_A)
        assert explicit["data"]["operation_id"] == OP_A
        other = await call("update_item", id="t1", title="Three")
        assert other["data"]["operation_id"] != generated

    _over_mcp(tmp_path, scenario)
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    found = git_backend.reconcile(ref, ids["created"], cache_dir=tmp_path / "cache2")
    assert found.state == "applied"
    log = subprocess.run(
        ["git", "-C", str(_remote(tmp_path)), "log", "todo-state", "--format=%B"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert f"Todo-Ack: {ids['created']}" in log or ids["created"] in log
