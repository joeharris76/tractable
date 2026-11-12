"""The instructions surface: a resource, a portable fallback tool, and a prompt.

All three return the same workflow text, rendered by
:func:`tractable.mcp.instructions.render_instructions` with a section naming the bound store.
Clients differ in what they support, so the text is offered three ways:

- ``tractable://instructions`` resource   -- the portable path.
- ``get_instructions`` tool          -- the portable fallback for clients that
  do not surface resources or prompts (and the only tool this foundation ships).
- ``tractable/workflow`` prompt           -- additive; Claude Code surfaces it as a
  slash command, other clients ignore it.

``get_instructions`` doubles as the first-tool-call seam that pins the principal
from the ``initialize`` handshake when no ``--actor`` was supplied.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import Context, FastMCP

from ..errors import E_STATE
from ..service import err
from .identity import PrincipalHolder
from .instructions import render_instructions
from .target import ResolvedTarget

_RESOURCE_URI = "tractable://instructions"
LOG = logging.getLogger("tractable.mcp")


def _client_info(ctx: Context | None):
    if ctx is None:
        return None
    try:
        return ctx.request_context.session.client_params.clientInfo
    except AttributeError:
        return None


def _path_segments(location: str) -> list[str]:
    """Lower-cased path segments of a remote or an ``expect_store`` value.

    The URL scheme, credentials and host are dropped, as are a trailing
    ``.git`` and empty segments, so ``https://github.com/Owner/Repo.git``,
    ``git@github.com:Owner/Repo`` and ``Owner/Repo`` all give ``["owner", "repo"]``.
    """
    text = location.strip()
    if "://" in text:
        text = urlsplit(text).path
    elif "@" in text.partition("/")[0] and ":" in text:
        # scp-like ``user@host:owner/repo``; a local path never has an
        # ``@`` before its first slash.
        text = text.partition(":")[2]
    segments = [part for part in text.lower().split("/") if part]
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][: -len(".git")]
    return [part for part in segments if part]


def _check_expected_store(target: ResolvedTarget, expected: str) -> dict[str, Any] | None:
    """None when *expected* names the bound remote's repository exactly.

    *expected* is ``owner/repo`` or a bare ``repo`` name, optionally as a full URL
    or with ``.git``; it is compared case-insensitively with the trailing path
    segments of the remote, whole segments only, so ``benchbox`` does not accept
    ``benchbox-site``. The branch is never compared: every store's default branch
    is ``todo-state``.
    """
    wanted = _path_segments(expected)
    if not wanted:
        return err(E_STATE, "expect_store must not be empty")
    actual = target.state_ref.display
    have = _path_segments(target.state_ref.display_remote)
    if len(wanted) <= len(have) and have[len(have) - len(wanted):] == wanted:
        return None
    return err(
        E_STATE,
        f"bound store {actual} does not match expected {expected.strip()!r}",
        recovery=[
            "do not write to this tracker",
            "restart tractable-mcp with the project's --config, or launch it from the project directory",
        ],
        kind="gate",
    )


def register_instructions(
    server: FastMCP, target: ResolvedTarget, principal: PrincipalHolder | None = None,
) -> None:
    def _pin_principal(ctx: Context | None) -> None:
        if principal is None or not principal.pending:
            return
        pinned = principal.ensure(_client_info(ctx))
        if pinned is not None:
            LOG.info("principal: %s (derived from initialize clientInfo)", pinned)

    def _render() -> str:
        return render_instructions(target, principal.principal if principal is not None else None)

    @server.resource(
        _RESOURCE_URI,
        name="tractable-instructions",
        title="tractable agent workflow",
        description="The autonomous-agent workflow protocol for the tractable tracker.",
        mime_type="text/markdown",
    )
    def _instructions_resource() -> str:
        return _render()

    @server.tool(
        name="get_instructions",
        description=(
            "Return the tractable autonomous-agent workflow protocol as Markdown, including the "
            "bound state store. Pass expect_store, the work's repository "
            "(owner/repo or name, matched exactly), to refuse a tracker for another project. Portable fallback for the "
            "tractable://instructions resource and the tractable/workflow prompt."
        ),
    )
    def get_instructions(
        expect_store: str | None = None, ctx: Context = None,  # type: ignore[assignment]
    ) -> str | dict[str, Any]:
        _pin_principal(ctx)
        if expect_store is not None:
            mismatch = _check_expected_store(target, expect_store)
            if mismatch is not None:
                return mismatch
        return _render()

    @server.prompt(
        name="tractable/workflow",
        title="tractable agent workflow",
        description="The autonomous-agent workflow protocol for the tractable tracker.",
    )
    def _workflow_prompt() -> str:
        return _render()
