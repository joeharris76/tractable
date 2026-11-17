"""Detect that the installed package moved on while this server kept running."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Any

from .. import TOOL_VERSION

_CACHE_SECONDS = 5.0
_cache: tuple[Callable[[], str], float, str | None] | None = None


def installed_version() -> str:
    return version("tractable")


def _installed_version_cached() -> str | None:
    global _cache
    lookup = installed_version
    now = time.monotonic()
    if _cache is not None and _cache[0] is lookup and now - _cache[1] < _CACHE_SECONDS:
        return _cache[2]
    try:
        found: str | None = lookup()
    except Exception:
        found = None
    _cache = (lookup, now, found)
    return found


def stale_warning() -> str | None:
    """A restart warning when the installed version differs from the running one."""
    installed = _installed_version_cached()
    if not installed or installed == TOOL_VERSION:
        return None
    return (
        f"This tractable-mcp server is running {TOOL_VERSION} but {installed} is now installed. "
        "Restart the session to load the installed version."
    )


def instructions_banner() -> str:
    warning = stale_warning()
    return f"## Server out of date\n\n{warning}\n\n" if warning else ""


def with_warnings(
    fn: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Add a top-level ``warnings`` list to a write response when the install changed."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await fn(*args, **kwargs)
        warning = stale_warning()
        if warning and isinstance(result, dict):
            result = {**result, "warnings": [warning]}
        return result

    return wrapper
