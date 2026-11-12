"""update_item over MCP: clearing fields, clearing a hold, and appending to context."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
from mcp.shared.memory import create_connected_server_and_client_session as connect

from tractable.git_backend import StateRef, bootstrap
from tractable.mcp.server import build_parser, build_server, resolve_launch_config
from tractable.service import TrackerService

FAR = "2099-01-01T00:00:00Z"


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


def _over_mcp(tmp_path: Path, scenario) -> None:
    async def go() -> None:
        argv = ["--state-remote", str(_remote(tmp_path)), "--cache-dir", str(tmp_path / "cache"), "--actor", "w1"]
        server = build_server(resolve_launch_config(build_parser().parse_args(argv)))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:

            async def call(name: str, **arguments) -> dict:
                return _payload(await session.call_tool(name, arguments))

            await scenario(call)

    anyio.run(go)


def _service(tmp_path: Path, worker: str) -> TrackerService:
    ref = StateRef(remote=str(_remote(tmp_path)), branch="todo-state")
    return TrackerService(ref=ref, cache_dir=tmp_path / f"cache-{worker}", worker=worker)


def test_empty_not_before_clears_a_hold_over_mcp(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="held", title="Held", not_before=FAR))["ok"]
        refused = await call("take", id="held")
        assert refused["code"] == "E_NOTHING_READY"

        cleared = await call("update_item", id="held", not_before="")

        assert cleared["ok"], cleared
        shown = (await call("show_item", id="held"))["data"]
        assert "waiting_until" not in shown
        assert shown["ready"] is True
        assert (await call("take", id="held"))["ok"]

    _over_mcp(tmp_path, scenario)


def test_clearing_a_hold_in_the_same_call_as_a_context_change(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="held", title="Held", not_before=FAR))["ok"]

        updated = await call("update_item", id="held", not_before="", context="released early")

        assert updated["ok"], updated
        assert set(updated["data"]["changed"]) == {"context", "not_before"}
        shown = (await call("show_item", id="held"))["data"]
        assert shown["context"] == "released early"
        assert "waiting_until" not in shown

    _over_mcp(tmp_path, scenario)


def test_an_update_that_leaves_not_before_out_keeps_the_hold(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="held", title="Held", not_before=FAR))["ok"]

        assert (await call("update_item", id="held", context="note"))["ok"]

        assert (await call("take", id="held"))["code"] == "E_NOTHING_READY"
        assert (await call("show_item", id="held"))["data"]["waiting_until"] == FAR

    _over_mcp(tmp_path, scenario)


def test_empty_string_clears_context_and_description_and_is_recorded(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="item", title="Item", context="CTX", description="DESC"))["ok"]

        assert (await call("update_item", id="item", context=""))["ok"]
        assert (await call("update_item", id="item", description=""))["ok"]

        shown = (await call("show_item", id="item"))["data"]
        assert not shown.get("context")
        assert not shown.get("description")
        assert shown["sessions"]["last"]["op"] == "update"
        assert shown["sessions"]["total"] == 3

    _over_mcp(tmp_path, scenario)


def test_an_update_that_supplies_no_field_is_refused(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="item", title="Item", context="keep"))["ok"]

        nothing = await call("update_item", id="item")

        assert not nothing["ok"]
        assert nothing["code"] == "E_STATE"
        assert "no state change" in nothing["error"]
        assert (await call("show_item", id="item"))["data"]["context"] == "keep"

    _over_mcp(tmp_path, scenario)


def test_context_append_adds_to_the_end_and_keeps_what_is_there(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="item", title="Item"))["ok"]

        assert (await call("update_item", id="item", context_append="first note"))["ok"]
        assert (await call("show_item", id="item"))["data"]["context"] == "first note"

        assert (await call("update_item", id="item", context_append="second note"))["ok"]
        assert (await call("show_item", id="item"))["data"]["context"] == "first note\n\nsecond note"

    _over_mcp(tmp_path, scenario)


def test_context_append_cannot_be_combined_with_context_or_be_empty(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="item", title="Item", context="keep"))["ok"]

        both = await call("update_item", id="item", context="replace", context_append="add")
        blank = await call("update_item", id="item", context_append="   ")

        assert both["code"] == "E_STATE" and not both["ok"]
        assert blank["code"] == "E_STATE" and not blank["ok"]
        assert (await call("show_item", id="item"))["data"]["context"] == "keep"

    _over_mcp(tmp_path, scenario)


def test_appended_notes_from_two_workers_are_both_kept(tmp_path: Path) -> None:
    first = _service(tmp_path, "worker-a")
    second = _service(tmp_path, "worker-b")
    assert first.create_item("item", "Item", context="base")["ok"]
    assert first.show_item("item")["ok"]

    assert second.update_item("item", context_append="from b")["ok"]
    assert first.update_item("item", context_append="from a")["ok"]

    assert second.show_item("item")["data"]["context"] == "base\n\nfrom b\n\nfrom a"


def test_clear_not_before_flag_clears_a_hold_without_an_empty_string(tmp_path: Path) -> None:
    async def scenario(call) -> None:
        assert (await call("create_item", id="held", title="Held", not_before=FAR))["ok"]
        assert (await call("take", id="held"))["code"] == "E_NOTHING_READY"

        both = await call("update_item", id="held", not_before=FAR, clear_not_before=True)
        assert not both["ok"] and "not both" in both["error"]
        assert (await call("show_item", id="held"))["data"]["waiting_until"] == FAR

        cleared = await call("update_item", id="held", clear_not_before=True, context_append="released")

        assert cleared["ok"], cleared
        assert set(cleared["data"]["changed"]) == {"context", "not_before"}
        shown = (await call("show_item", id="held"))["data"]
        assert "waiting_until" not in shown
        assert (await call("take", id="held"))["ok"]

    _over_mcp(tmp_path, scenario)
