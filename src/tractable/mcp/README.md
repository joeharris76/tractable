# `tractable.mcp` — the MCP stdio server

This package is the agent interface for the tracker (ADR 0007). It builds the
server, resolves the state branch target, pins the worker identity, and
registers eighteen task tools plus `get_instructions` over shared service operations.

## Layout

| File | Purpose |
| --- | --- |
| `__init__.py` | Package doc and a `main` shim. |
| `__main__.py` | `python -m tractable.mcp` → `server.main()`. |
| `server.py` | Launch args, stderr logging, `LaunchConfig`, `build_server`, lifespan, `startup_check`, `main`. |
| `target.py` | State target resolution (flag > env > upward discovery), pinned for the process lifetime. |
| `identity.py` | Worker identity (`--actor` → `TRACTABLE_ACTOR` → instance-scoped MCP fallback) and the per-process session id. |
| `tools.py` | The eighteen task tools: `list_items`, `show_item`, `create_item`, `register_batch`, `update_item`, `take`, `takeover`, `release`, `prepare`, `bind_batch_pr`, `abort_batch`, `finish`, `renew`, `drop`, `list_deferrals`, `defer`, `promote_deferral`, `dismiss_deferral`. |
| `resources.py` | `tractable://instructions` resource, `get_instructions` tool, and `tractable/workflow` prompt — all the same text. |
| `instructions.py` | The workflow protocol text. |

## Tools

One server instance is one logical worker. Default identities are isolated per
server; stable explicit actors must not run concurrently. Blocking Git work
runs via `asyncio.to_thread`; there is no shared mutable connection to guard.

The first `register_batch` call that upgrades a state branch to schema 4 needs
`confirm_schema3_cutover=true` (ADR 0012). Set it only after stopping or
upgrading every older process with access to that branch and releasing other
workers' claims; older clients refuse a schema-4 store with `E_SCHEMA`.
`initialize` advertises `capabilities.experimental.tractable_batch` (version 2,
receipt schema `prepared_work_v2`) so consumers can refuse a runtime without
batch v2.

`create_item` and `update_item` accept `not_before`, a future RFC 3339 time
with `Z` or an offset. It holds an open task out of the ready queue until that
time, and `take` and `finish` refuse the task until then; `not_before=""`
or `clear_not_before=true` on `update_item` clears it (use the flag if a client
drops empty-string arguments). The field lives in the detail file and does not change the schema
version, so an older server loads and keeps it but does not enforce the hold.
Upgrade every server with access to the branch before relying on it.

## SDK pin

`mcp>=1.10.0,<2`. The 2.x major is a breaking rename
(`mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`, and
`mcp.shared.memory` changed) while this package is written against the FastMCP
API. The wire protocol is unchanged across that major, so client interop is
unaffected by staying on 1.x.

`FastMCP` does not forward a version to the lowlevel server, so `server.py`
sets `_mcp_server.version` directly; otherwise `serverInfo` reports the SDK's
version to every client instead of the tracker's.

## Snapshots

`scripts/mcp_snapshots/tools.json` freezes the registered tool names,
descriptions, and input schemas. `tests/test_mcp_stdio.py` compares the live
server against it, so any tool change must land with a regenerated snapshot.
