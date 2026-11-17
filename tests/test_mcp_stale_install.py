"""A running server warns when the installed package differs from the one it loaded."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from tractable import TOOL_VERSION
from tractable.mcp import staleness
from tractable.mcp.server import build_server

from test_mcp_slim import _launch, _payload


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(staleness, "_cache", None)


def _instructions(result) -> str:
    return "".join(getattr(b, "text", "") or "" for b in result.content)


def _run(tmp_path: Path, installed):
    seen: dict = {}

    async def go() -> None:
        server = build_server(_launch(tmp_path))
        async with connect(server) as session:
            seen["instructions"] = _instructions(await session.call_tool("get_instructions", {}))
            seen["write"] = _payload(await session.call_tool("create_item", {"id": "t1", "title": "First"}))
            seen["read"] = _payload(await session.call_tool("list_items", {}))

    staleness_lookup = installed
    return go, seen, staleness_lookup


def test_warns_when_installed_version_differs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(staleness, "installed_version", lambda: "99.0.0")
    go, seen, _ = _run(tmp_path, None)
    anyio.run(go)
    assert seen["instructions"].startswith("## Server out of date")
    assert TOOL_VERSION in seen["instructions"] and "99.0.0" in seen["instructions"]
    assert "Restart the session" in seen["instructions"]
    assert seen["write"]["ok"]
    [warning] = seen["write"]["warnings"]
    assert TOOL_VERSION in warning and "99.0.0" in warning and "Restart the session" in warning
    assert "warnings" not in seen["read"]


def test_no_warning_when_versions_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(staleness, "installed_version", lambda: TOOL_VERSION)
    go, seen, _ = _run(tmp_path, None)
    anyio.run(go)
    assert "Server out of date" not in seen["instructions"]
    assert seen["write"]["ok"] and "warnings" not in seen["write"]


@pytest.mark.parametrize("exc", [PackageNotFoundError("tractable"), OSError("unreadable"), ValueError("bad")])
def test_no_warning_when_lookup_fails(tmp_path: Path, monkeypatch, exc) -> None:
    def broken() -> str:
        raise exc

    monkeypatch.setattr(staleness, "installed_version", broken)
    go, seen, _ = _run(tmp_path, None)
    anyio.run(go)
    assert "Server out of date" not in seen["instructions"]
    assert seen["write"]["ok"] and "warnings" not in seen["write"]
