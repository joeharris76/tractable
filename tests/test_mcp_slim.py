"""Slim MCP proving cases: lifecycle, serialization caps, schema cost."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
from mcp.shared.memory import create_connected_server_and_client_session as connect

from tractable import count_tokens
from tractable import store
from tractable.git_backend import StateRef, bootstrap
from tractable.mcp.instructions import INSTRUCTIONS
from tractable.mcp.server import build_parser, build_server, resolve_launch_config
from tractable.service import MAX_BYTES


def _launch(tmp_path: Path, actor: str | None = "w1", session: str | None = None):
    remote = tmp_path / "state.git"
    if not remote.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
        bootstrap(StateRef(remote=str(remote), branch="todo-state"))
    parser = build_parser()
    argv = ["--state-remote", str(remote), "--cache-dir", str(tmp_path / "cache")]
    if actor is not None:
        argv += ["--actor", actor]
    if session is not None:
        argv += ["--session", session]
    return resolve_launch_config(parser.parse_args(argv))


def _payload(result) -> dict:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    raise AssertionError(f"no JSON payload in {result!r}")


def _text_len(result) -> int:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return len(text.encode("utf-8"))
    return 0


def _make_real_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "delivery-repo"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "base"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, revision


def test_lifecycle_over_tools(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            created = _payload(await session.call_tool("create_item", {"id": "t1", "title": "First"}))
            assert created["ok"], created
            took = _payload(await session.call_tool("take", {"id": "t1"}))
            assert took["ok"], took
            gen = took["data"]["claim"]["generation"]
            # Take returns enough context to begin work: no immediate show needed.
            assert took["data"]["title"] == "First"
            assert "description_excerpt" in took["data"]
            shown = _payload(await session.call_tool("show_item", {"id": "t1"}))
            assert shown["ok"] and shown["data"]["status"] == "active"
            renewed = _payload(await session.call_tool("renew", {"id": "t1", "generation": gen}))
            assert renewed["ok"], renewed
            finished = _payload(await session.call_tool("finish", {"id": "t1", "generation": gen}))
            assert finished["ok"] and finished["data"]["status"] == "done"
            dropped = _payload(await session.call_tool("create_item", {"id": "t2", "title": "Drop me"}))
            assert dropped["ok"]
            gone = _payload(await session.call_tool("drop", {"id": "t2", "reason": "not needed"}))
            assert gone["ok"] and gone["data"]["status"] == "dropped"

    anyio.run(go)


def test_registered_batch_contract_is_public_over_mcp(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "README").write_text("batch\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "base"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    scope = {"a": ["src/**"]}

    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            registered = _payload(await session.call_tool("register_batch", {
                "batch_id": "batch-1", "project_id": "tractable", "repository": str(repo),
                "owner_generation": "1" * 32, "integration_branch": "main",
                "integration_worktree": str(repo), "start_head": revision,
                "members": ["a"], "scope": scope, "scope_hash": store.scope_digest(scope),
                "delivery_boundary": "final-pr", "terminal_outcome": "merged",
                "confirm_schema3_cutover": True,
            }))
            assert registered["ok"], registered
            created = _payload(await session.call_tool("create_item", {
                "id": "a", "title": "A",
                "batch": {"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
            }))
            assert created["ok"], created
            shown = _payload(await session.call_tool("show_item", {"id": "a"}))
            assert shown["ok"] and shown["data"]["batch"]["batch_id"] == "batch-1"

    anyio.run(go)


def test_public_mcp_prepared_delivery_and_finish(tmp_path: Path) -> None:
    repo, base = _make_real_repo(tmp_path)
    worker = tmp_path / "worker-a"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "worker-a", str(worker), base], check=True)
    (worker / "a").mkdir()
    (worker / "a" / "file").write_text("A\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worker), "add", "a/file"], check=True)
    subprocess.run(["git", "-C", str(worker), "commit", "--quiet", "-m", "A"], check=True)
    ahead = subprocess.check_output(["git", "-C", str(worker), "rev-parse", "HEAD"], text=True).strip()
    scope = {"a": ["a/**"]}

    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            registered = _payload(await session.call_tool("register_batch", {
                "batch_id": "delivery", "project_id": "tractable", "repository": str(repo),
                "owner_generation": "1" * 32, "integration_branch": "main",
                "integration_worktree": str(repo), "start_head": base, "members": ["a"],
                "scope": scope, "scope_hash": store.scope_digest(scope),
                "delivery_boundary": "final-pr", "terminal_outcome": "merged",
                "confirm_schema3_cutover": True,
            }))
            assert registered["ok"], registered
            subprocess.run(["git", "-C", str(repo), "merge", "--ff-only", "worker-a"], check=True)
            created = _payload(await session.call_tool("create_item", {
                "id": "a", "title": "A",
                "batch": {"batch_id": "delivery", "member_id": "a", "implementation_dependencies": []},
            }))
            assert created["ok"], created
            taken = _payload(await session.call_tool("take", {"id": "a"}))
            generation = taken["data"]["claim"]["generation"]
            premature_finish = _payload(await session.call_tool("finish", {
                "id": "a", "generation": generation,
            }))
            assert not premature_finish["ok"] and premature_finish["code"] == "E_FINAL_EVIDENCE"
            stale = _payload(await session.call_tool("prepare", {
                "id": "a", "generation": "0" * 32, "batch_id": "delivery", "member_id": "a",
                "source_worktree": str(worker), "source_revision": ahead, "source_base": base,
                "accepted_head": ahead, "integration_head": ahead,
                "scope_hash": store.scope_digest(scope), "verification": {"status": "passed", "revision": ahead,
                "clean": True, "suite": "bounded", "command": ["human-run"]},
            }))
            assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE"
            wrong_head = _payload(await session.call_tool("prepare", {
                "id": "a", "generation": generation, "batch_id": "delivery", "member_id": "a",
                "source_worktree": str(worker), "source_revision": ahead, "source_base": base,
                "accepted_head": ahead, "integration_head": base,
                "scope_hash": store.scope_digest(scope), "verification": {"status": "passed", "revision": ahead,
                "clean": True, "suite": "bounded", "command": ["human-run"]},
            }))
            assert not wrong_head["ok"]
            prepared = _payload(await session.call_tool("prepare", {
                "id": "a", "generation": generation, "batch_id": "delivery", "member_id": "a",
                "source_worktree": str(worker), "source_revision": ahead, "source_base": base,
                "accepted_head": ahead, "integration_head": ahead,
                "scope_hash": store.scope_digest(scope), "verification": {
                    "status": "passed", "revision": ahead, "clean": True, "suite": "bounded",
                    "command": ["uv", "run", "--", "pytest", "tests/test_batch_delivery.py", "-q"],
                    "review": {"outcome": "clear"},
                },
            }))
            assert prepared["ok"] and prepared["data"]["status"] == "open", prepared
            taken = _payload(await session.call_tool("take", {"id": "a"}))
            generation = taken["data"]["claim"]["generation"]
            final_pr = {"repository": "owner/repo", "number": 1, "node_id": "PR_delivery", "head": ahead}
            assert _payload(await session.call_tool("bind_batch_pr", {
                "batch_id": "delivery", "owner_generation": "1" * 32,
                "pr_repository": "owner/repo", "number": 1, "node_id": "PR_delivery", "head": ahead,
            }))["ok"]
            malformed = _payload(await session.call_tool("finish", {
                "id": "a", "generation": generation, "final_evidence": {
                    "status": "passed", "batch_id": "delivery", "project_id": "tractable",
                    "repository": str(repo), "tree_worktree": str(repo), "tree_revision": ahead,
                    "integration_branch": "main", "integration_head": ahead,
                    "scope_hash": store.scope_digest(scope), "suite": "combined", "clean": True,
                },
            }))
            assert not malformed["ok"] and malformed["code"] == "E_FINAL_EVIDENCE"
            finished = _payload(await session.call_tool("finish", {
                "id": "a", "generation": generation, "final_evidence": {
                    "status": "passed", "batch_id": "delivery", "project_id": "tractable",
                    "repository": str(repo), "tree_worktree": str(repo), "tree_revision": ahead,
                    "integration_branch": "main", "integration_head": ahead,
                    "scope_hash": store.scope_digest(scope), "member_heads": {"a": ahead},
                    "member_ranges": {"a": {"base": base, "head": ahead}},
                    "changed_files": ["a/file"], "final_pr": final_pr,
                    "suite": "combined", "clean": True,
                },
            }))
            assert finished["ok"] and finished["data"]["status"] == "done", finished
            aborted = _payload(await session.call_tool("abort_batch", {
                "batch_id": "delivery", "owner_generation": "1" * 32,
            }))
            assert not aborted["ok"]

    anyio.run(go)


def test_second_worker_conflict_and_stale_generation(tmp_path: Path) -> None:
    async def go():
        # One server instance pins one principal, so distinct workers run
        # distinct server instances against the same state branch.
        server1 = build_server(_launch(tmp_path, actor="w1"))
        server2 = build_server(_launch(tmp_path, actor="w2"))
        async with connect(server1, client_info=types.Implementation(name="w1", version="0")) as s1:
            assert _payload(await s1.call_tool("create_item", {"id": "c", "title": "Contested"}))["ok"]
            took = _payload(await s1.call_tool("take", {"id": "c"}))
            gen = took["data"]["claim"]["generation"]
        async with connect(server2, client_info=types.Implementation(name="w2", version="0")) as s2:
            conflict = _payload(await s2.call_tool("take", {"id": "c"}))
            assert not conflict["ok"] and conflict["code"] == "E_CONFLICT"
            stale = _payload(await s2.call_tool("finish", {"id": "c", "generation": "0" * 32}))
            assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE"
        async with connect(server1, client_info=types.Implementation(name="w1", version="0")) as s1:
            released = _payload(await s1.call_tool("release", {"id": "c", "generation": gen}))
            assert released["ok"]

    anyio.run(go)


def test_independent_same_name_clients_have_separate_claim_ownership(tmp_path: Path) -> None:
    async def go():
        server1 = build_server(_launch(tmp_path, actor=None))
        server2 = build_server(_launch(tmp_path, actor=None))
        same_client = types.Implementation(name="codex-mcp-client", version="0")
        async with connect(server1, client_info=same_client) as s1:
            assert _payload(await s1.call_tool("create_item", {"id": "a", "title": "Task A"}))["ok"]
            assert _payload(await s1.call_tool("create_item", {"id": "b", "title": "Task B"}))["ok"]
            first = _payload(await s1.call_tool("take", {"id": "a"}))
            assert first["ok"], first
            first_generation = first["data"]["claim"]["generation"]

        async with connect(server2, client_info=same_client) as s2:
            conflict = _payload(await s2.call_tool("take", {"id": "a"}))
            assert not conflict["ok"] and conflict["code"] == "E_CONFLICT"
            for tool in ("renew", "finish", "release", "drop"):
                stale = _payload(await s2.call_tool(tool, {"id": "a", "generation": first_generation, **({"reason": "r"} if tool == "drop" else {})}))
                assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE", (tool, stale)
            second = _payload(await s2.call_tool("take", {"id": "b"}))
            assert second["ok"], second

    anyio.run(go)


def test_restart_with_same_session_re_adopts_and_stales_old_generation(tmp_path: Path) -> None:
    async def go():
        same_client = types.Implementation(name="codex-mcp-client", version="0")
        server1 = build_server(_launch(tmp_path, actor=None, session="resume-123"))
        server2 = build_server(_launch(tmp_path, actor=None, session="resume-123"))
        async with connect(server1, client_info=same_client) as s1:
            assert _payload(await s1.call_tool("create_item", {"id": "resume", "title": "Resume"}))["ok"]
            assert _payload(await s1.call_tool("create_item", {"id": "other", "title": "Other"}))["ok"]
            first = _payload(await s1.call_tool("take", {"id": "resume"}))
            old_generation = first["data"]["claim"]["generation"]

        async with connect(server2, client_info=same_client) as s2:
            adopted = _payload(await s2.call_tool("take", {"id": "resume"}))
            assert adopted["ok"], adopted
            new_generation = adopted["data"]["claim"]["generation"]
            assert new_generation != old_generation
            second_claim = _payload(await s2.call_tool("take", {"id": "other"}))
            assert not second_claim["ok"] and second_claim["code"] == "E_MULTIPLE_CLAIMS"
            assert any("--actor" in step for step in second_claim["recovery"])

        async with connect(server1, client_info=same_client) as s1:
            stale = _payload(await s1.call_tool("renew", {"id": "resume", "generation": old_generation}))
            assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE"

    anyio.run(go)


def test_explicit_actor_restart_re_adopts_without_weakening_one_claim_rule(tmp_path: Path) -> None:
    async def go():
        same_client = types.Implementation(name="codex-mcp-client", version="0")
        server1 = build_server(_launch(tmp_path, actor="stable-worker"))
        server2 = build_server(_launch(tmp_path, actor="stable-worker"))
        async with connect(server1, client_info=same_client) as s1:
            assert _payload(await s1.call_tool("create_item", {"id": "owned", "title": "Owned"}))["ok"]
            assert _payload(await s1.call_tool("create_item", {"id": "other", "title": "Other"}))["ok"]
            first = _payload(await s1.call_tool("take", {"id": "owned"}))
            old_generation = first["data"]["claim"]["generation"]

        async with connect(server2, client_info=same_client) as s2:
            adopted = _payload(await s2.call_tool("take", {"id": "owned"}))
            assert adopted["ok"], adopted
            assert adopted["data"]["claim"]["generation"] != old_generation
            blocked = _payload(await s2.call_tool("take", {"id": "other"}))
            assert not blocked["ok"] and blocked["code"] == "E_MULTIPLE_CLAIMS"

    anyio.run(go)


def test_every_tool_result_fits_final_serialization(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            big = "y" * 50000
            assert _payload(await session.call_tool(
                "create_item", {"id": "big", "title": "Big", "description": big}))["ok"]
            for name, args in (
                ("list_items", {}),
                ("list_items", {"limit": 50}),
                ("show_item", {"id": "big"}),
                ("show_item", {"id": "big", "field": "description"}),
                ("update_item", {"id": "big", "title": "Big v2"}),
                ("take", {"id": "big"}),
            ):
                result = await session.call_tool(name, args)
                assert _text_len(result) <= MAX_BYTES, name

    anyio.run(go)


def test_token_targets_on_actual_surfaces(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            for i in range(5):
                assert _payload(await session.call_tool(
                    "create_item", {"id": f"r{i}", "title": f"Row task {i}"}))["ok"]
            listed = await session.call_tool("list_items", {})
            tokens, _ = count_tokens(json.dumps(_payload(listed)))
            assert tokens <= 600, tokens
            took = await session.call_tool("take", {"id": "r0"})
            take_tokens, _ = count_tokens(json.dumps(_payload(took)))
            assert take_tokens <= 1600, take_tokens
            gen = _payload(took)["data"]["claim"]["generation"]
            finished = await session.call_tool("finish", {"id": "r0", "generation": gen})
            ack_tokens, _ = count_tokens(json.dumps(_payload(finished)))
            assert ack_tokens <= 100, ack_tokens

    anyio.run(go)


def test_deferral_verbs_over_tools(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            assert _payload(await session.call_tool(
                "create_item", {"id": "d", "title": "Deferred work"}))["ok"]
            deferred = _payload(await session.call_tool(
                "defer", {"id": "d", "summary": "Later", "reason": "needs infra"}))
            assert deferred["ok"] and deferred["data"]["deferral"]["id"] == 1, deferred
            queued = _payload(await session.call_tool("list_deferrals", {}))
            assert queued["ok"] and queued["data"]["total"] == 1, queued
            took = _payload(await session.call_tool("take", {"id": "d"}))
            assert took["ok"], took
            blocked = _payload(await session.call_tool(
                "finish", {"id": "d", "generation": took["data"]["claim"]["generation"]}))
            assert not blocked["ok"] and blocked["code"] == "E_OPEN_DEFERRALS", blocked
            promoted = _payload(await session.call_tool(
                "promote_deferral", {"id": "d", "deferral_id": 1}))
            assert promoted["ok"], promoted
            assert promoted["data"]["resolved_item"] == "d-deferral-1"
            empty = _payload(await session.call_tool("list_deferrals", {}))
            assert empty["ok"] and empty["data"]["total"] == 0, empty
            assert _payload(await session.call_tool("defer", {
                "id": "d", "summary": "More", "reason": "waiting"}))["ok"]
            dismissed = _payload(await session.call_tool(
                "dismiss_deferral", {"id": "d", "deferral_id": 2, "reason": "not needed"}))
            assert dismissed["ok"], dismissed
            finished = _payload(await session.call_tool(
                "finish", {"id": "d", "generation": took["data"]["claim"]["generation"]}))
            assert finished["ok"], finished

    anyio.run(go)


def test_startup_schema_cost_is_small(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="x", version="0")) as session:
            tools = await session.list_tools()
            schema_blob = json.dumps([{"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                                      for t in tools.tools])
            tokens, tokenizer = count_tokens(schema_blob + INSTRUCTIONS)
            assert tokenizer == "o200k_base"
            # 19 tools plus the full protocol guidance, against 3,344 for
            # the old 26 tool definitions alone. Bumped for the audited
            # takeover tool (3-parameter schema plus one guidance line),
            # then for the four deferral verbs (list/defer/promote/dismiss
            # plus the finish-gate guidance): deliberate, reviewed surface,
            # still far below the old count. Bumped to 4,700 for the
            # expect_store check on get_instructions, the E_MULTIPLE_CLAIMS
            # guidance, and update_item's context_append with the field
            # semantics its description now states (4,440 before, 4,670 now).
            # The required `reason` parameter on drop and its description
            # and guidance line raised the measured total from 4,670 to
            # 4,744; the ceiling rises by that 74.
            # Batch v2 adds show_batch, advance_batch_head and
            # recover_batch_owner plus the owner/head guidance and the
            # revisable bind_batch_pr arguments (ADR 0012): 4,744 -> 5,614.
            # Guarded checkout relocation adds expected_integration_worktree/head
            # and integration_worktree to recover_batch_owner: 5,614 -> 5,745.
            # The per-batch claim guidance and the verification_path and
            # final_evidence_path arguments: 5,745 -> 5,907.
            # update_item's clear_not_before flag and the exact expect_store
            # wording: 5,907 -> 5,946.
            assert tokens <= 5950, tokens

    anyio.run(go)
