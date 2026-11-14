"""Release gate 1: a full lifecycle against an installed tractable, on a scratch remote.

    python scripts/release_gate1.py <bin-dir> <expected-version>

``<bin-dir>`` holds ``tractable``, ``tractable-mcp`` and the ``python`` that has
the ``mcp`` package importable; run this script with that python. Every write
goes to a bare remote created under a fresh temporary directory, which is
removed on exit. Prints one ``PASS|FAIL  <check>  <detail>`` line per check and
a final ``Gate 1: N/N checks passed``; exits non-zero when any check fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALL_TIMEOUT_S = 120
COMMAND_TIMEOUT_S = 120
GEN = "1" * 32
BATCH = "gate-batch"
STATE_AUTHOR = "tractable <tractable@localhost>"


class CheckFailed(Exception):
    pass


def expect(condition: object, detail: str) -> None:
    if not condition:
        raise CheckFailed(detail)


def one_line(text: object, limit: int = 220) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.total = 0

    def add(self, ok: bool, name: str, detail: str) -> None:
        self.total += 1
        self.passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {one_line(detail)}", flush=True)

    async def check(self, name: str, body: Any) -> Any:
        try:
            detail = await body()
        except CheckFailed as exc:
            self.add(False, name, str(exc))
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.add(False, name, f"{type(exc).__name__}: {exc}")
        else:
            self.add(True, name, detail or "ok")
            return detail
        return None


def child_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRACTABLE_", "GIT_"))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), PYTHONUNBUFFERED="1")
    return env


def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=COMMAND_TIMEOUT_S,
    )


def reply(result: Any) -> dict[str, Any]:
    text = "".join(part.text for part in result.content if getattr(part, "type", "") == "text")
    try:
        value = json.loads(text)
    except ValueError:
        return {"ok": not result.isError, "text": text}
    return value if isinstance(value, dict) else {"ok": not result.isError, "text": text}


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    with anyio.fail_after(CALL_TIMEOUT_S):
        return reply(await session.call_tool(tool, arguments))


def require_ok(response: dict[str, Any], what: str) -> dict[str, Any]:
    expect(response.get("ok"), f"{what} refused: {response.get('code')} {response.get('error') or response.get('text')}")
    return response.get("data") or {}


def require_refused(response: dict[str, Any], what: str, code: str | None = None) -> str:
    expect(not response.get("ok"), f"{what} was accepted")
    if code is not None:
        expect(response.get("code") == code, f"{what} refused with {response.get('code')!r}, wanted {code}")
    return str(response.get("code"))


class Gate:
    def __init__(self, bin_dir: Path, version: str, root: Path) -> None:
        self.bin = bin_dir
        self.version = version
        self.root = root
        self.env = child_env(root)
        self.remote = root / "repo-site.git"
        self.cache = root / "cache"
        self.stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.initialized: Any = None
        self.report = Report()
        self.created: set[str] = set()
        self.done = 0
        self.dropped = 0

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(
            [str(self.bin / "tractable"), *args, "--state-remote", str(self.remote), "--cache-dir", str(self.cache)],
            self.env, self.root,
        )

    async def start(self, worker: str) -> ClientSession:
        params = StdioServerParameters(
            command=str(self.bin / "tractable-mcp"),
            args=[
                "--repo-root", str(self.root), "--state-remote", str(self.remote),
                "--cache-dir", str(self.root / f"cache-{worker}"), "--actor", worker, "--log-level", "error",
            ],
            env=self.env, cwd=str(self.root),
        )
        errlog = self.stack.enter_context((self.root / f"server-{worker}.log").open("w", encoding="utf-8"))
        reader, writer = await self.stack.enter_async_context(stdio_client(params, errlog=errlog))
        session = await self.stack.enter_async_context(ClientSession(reader, writer))
        with anyio.fail_after(CALL_TIMEOUT_S):
            initialized = await session.initialize()
        self.sessions[worker] = session
        if worker == "alpha":
            self.initialized = initialized
        return session

    async def create(self, worker: str, item_id: str, **fields: Any) -> dict[str, Any]:
        out = require_ok(await call(self.sessions[worker], "create_item", id=item_id, title=item_id.upper(), **fields),
                         f"create_item {item_id}")
        self.created.add(item_id)
        return out

    async def run_all(self) -> None:
        report, version = self.report, self.version

        async def cli_version(name: str) -> str:
            out = run([str(self.bin / name), "--version"], self.env, self.root)
            expect(out.returncode == 0, f"exit {out.returncode}: {out.stderr}")
            expect(out.stdout.split()[-1:] == [version], f"printed {out.stdout.strip()!r}, wanted version {version}")
            return out.stdout.strip()

        await report.check("tractable --version", lambda: cli_version("tractable"))
        await report.check("tractable-mcp --version", lambda: cli_version("tractable-mcp"))

        async def bootstrap() -> str:
            subprocess.run(["git", "init", "--quiet", "--bare", str(self.remote)], env=self.env, check=True, timeout=60)
            out = self.cli("bootstrap")
            expect(out.returncode == 0, f"exit {out.returncode}: {out.stderr}")
            return "scratch bare remote bootstrapped"

        async def validate_empty() -> str:
            out = self.cli("validate")
            expect(out.returncode == 0, f"exit {out.returncode}: {out.stderr}")
            counts = json.loads(out.stdout)
            expect(counts["items"] == 0, f"fresh state holds {counts['items']} items")
            return f"items={counts['items']}"

        await report.check("bootstrap on scratch remote", bootstrap)
        await report.check("validate fresh state", validate_empty)

        async def server_start() -> str:
            await self.start("alpha")
            await self.start("beta")
            return "alpha and beta servers initialized"

        if await report.check("start MCP servers", server_start) is None:
            return
        info = self.initialized.serverInfo

        async def server_name() -> str:
            expect(info.name == "tractable", f"serverInfo.name is {info.name!r}")
            return info.name

        async def server_version() -> str:
            expect(info.version == version, f"serverInfo.version is {info.version!r}, wanted {version}")
            return info.version

        async def batch_capability() -> str:
            experimental = getattr(self.initialized.capabilities, "experimental", None) or {}
            capability = experimental.get("tractable_batch")
            expect(isinstance(capability, dict), "capabilities.experimental.tractable_batch is absent")
            expect(capability.get("tool_version") == version,
                   f"tractable_batch.tool_version is {capability.get('tool_version')!r}, wanted {version}")
            return f"capability version {capability.get('version')}, tool_version {capability['tool_version']}"

        await report.check("serverInfo name", server_name)
        await report.check("serverInfo version", server_version)
        await report.check("tractable_batch experimental capability", batch_capability)

        alpha, beta = self.sessions["alpha"], self.sessions["beta"]

        async def bound_store() -> str:
            out = await call(alpha, "get_instructions")
            expect(out.get("ok") and "Bound store" in out.get("text", ""), "instructions lack a Bound store section")
            expect(str(self.remote) in out["text"], "Bound store section does not name the scratch remote")
            return f"bound to {self.remote.name}"

        async def exact_store() -> str:
            out = await call(alpha, "get_instructions", expect_store=self.remote.stem)
            expect(out.get("ok") and "Bound store" in out.get("text", ""), f"exact name refused: {out.get('error')}")
            return f"expect_store={self.remote.stem!r} accepted"

        async def mismatch() -> str:
            code = require_refused(await call(alpha, "get_instructions", expect_store="another-project"),
                                   "expect_store for another project")
            return f"refused with {code}"

        async def prefix_sibling() -> str:
            prefix = self.remote.stem.partition("-")[0]
            code = require_refused(await call(alpha, "get_instructions", expect_store=prefix),
                                   f"expect_store={prefix!r} against {self.remote.name}")
            return f"{prefix!r} refused against {self.remote.name} with {code}"

        await report.check("bound store", bound_store)
        await report.check("expect_store exact name accepted", exact_store)
        await report.check("expect_store mismatch refused", mismatch)
        await report.check("expect_store prefix sibling refused", prefix_sibling)

        claim: dict[str, str] = {}

        async def create_take() -> str:
            await self.create("alpha", "gate-a")
            data = require_ok(await call(alpha, "take", id="gate-a"), "take gate-a")
            claim["gate-a"] = data["claim"]["generation"]
            return f"generation {claim['gate-a'][:8]}"

        async def renew() -> str:
            data = require_ok(await call(alpha, "renew", id="gate-a", generation=claim["gate-a"]), "renew gate-a")
            return one_line(data.get("claim") or data)

        async def second_ordinary_take() -> str:
            await self.create("alpha", "gate-b")
            code = require_refused(await call(alpha, "take", id="gate-b"), "second ordinary take", "E_MULTIPLE_CLAIMS")
            return f"refused with {code}"

        async def second_worker() -> str:
            take = require_refused(await call(beta, "take", id="gate-a"), "take of a live claim by a second worker")
            finish = require_refused(
                await call(beta, "finish", id="gate-a", generation=claim["gate-a"]),
                "finish of a live claim by a second worker",
            )
            return f"take refused with {take}, finish refused with {finish}"

        async def write_on_top() -> str:
            async def note(session: ClientSession, text: str) -> None:
                require_ok(await call(session, "update_item", id="gate-a", context_append=text), f"context_append {text}")

            async with anyio.create_task_group() as group:
                group.start_soon(note, alpha, "note from alpha")
                group.start_soon(note, beta, "note from beta")
            data = require_ok(await call(alpha, "show_item", id="gate-a", field="context"), "show_item gate-a context")
            text = json.dumps(data)
            expect("note from alpha" in text and "note from beta" in text, f"a concurrent note was lost: {one_line(text)}")
            held = require_ok(await call(alpha, "show_item", id="gate-a"), "show_item gate-a")
            expect(held.get("claim", {}).get("worker") == "alpha", f"claim changed hands: {held.get('claim')}")
            return "both concurrent notes kept, claim still alpha's"

        await report.check("create_item and take", create_take)
        await report.check("renew", renew)
        await report.check("second ordinary take refused E_MULTIPLE_CLAIMS", second_ordinary_take)
        await report.check("second worker refused the live claim", second_worker)
        await report.check("concurrent context_append from a second worker keeps both notes", write_on_top)

        async def concurrent_writers() -> str:
            failures: list[str] = []

            async def writer(worker: str) -> None:
                for index in range(3):
                    item_id = f"gate-{worker}-{index}"
                    try:
                        await self.create(worker, item_id)
                    except CheckFailed as exc:
                        failures.append(str(exc))

            async with anyio.create_task_group() as group:
                group.start_soon(writer, "alpha")
                group.start_soon(writer, "beta")
            expect(not failures, "; ".join(failures))
            return "6 concurrent creates from two writers all published"

        async def finish() -> str:
            require_ok(await call(alpha, "finish", id="gate-a", generation=claim["gate-a"]), "finish gate-a")
            self.done += 1
            return "gate-a done"

        async def drop() -> str:
            require_ok(await call(alpha, "drop", id="gate-b", reason="gate 1 drop path"), "drop gate-b")
            self.dropped += 1
            return "gate-b dropped with a reason"

        async def list_reflects() -> str:
            for name, session in (("alpha", alpha), ("beta", beta)):
                data = require_ok(await call(session, "list_items", limit=100), f"list_items by {name}")
                listed = {row["id"] for row in data["items"]}
                missing = sorted(self.created - listed)
                expect(not missing, f"{name} does not see {missing}")
            return f"both writers list all {len(self.created)} items"

        await report.check("two concurrent writers both publish", concurrent_writers)
        await report.check("finish", finish)
        await report.check("drop with a reason", drop)
        await report.check("list_items reflects every writer", list_reflects)

        async def clear_not_before() -> str:
            future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            await self.create("alpha", "gate-hold", not_before=future)

            async def ready() -> set[str]:
                data = require_ok(await call(alpha, "list_items", ready_only=True, limit=100), "list_items ready_only")
                return {row["id"] for row in data["items"]}

            expect("gate-hold" not in await ready(), "item with a future not_before is ready")
            require_ok(await call(alpha, "update_item", id="gate-hold", clear_not_before=True),
                       "update_item clear_not_before")
            expect("gate-hold" in await ready(), "item is not ready after clear_not_before")
            return "held item became ready"

        await report.check("update_item clear_not_before makes a held item ready", clear_not_before)

        repo = self.root / "scratch-repo"
        members = ["gate-m1", "gate-m2"]

        async def register() -> str:
            subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], env=self.env, check=True, timeout=60)
            (repo / "README").write_text("scratch\n", encoding="utf-8")
            git = ["git", "-C", str(repo), "-c", "user.name=gate", "-c", "user.email=gate@localhost"]
            subprocess.run([*git, "add", "README"], env=self.env, check=True, timeout=60)
            subprocess.run([*git, "commit", "--quiet", "-m", "scratch"], env=self.env, check=True, timeout=60)
            head = run(["git", "-C", str(repo), "rev-parse", "HEAD"], self.env, self.root).stdout.strip()
            from tractable.store import scope_digest

            scope = {member: [f"{member}/**"] for member in members}
            require_ok(
                await call(
                    alpha, "register_batch", batch_id=BATCH, project_id="gate1", repository=str(repo),
                    owner_generation=GEN, integration_branch="main", integration_worktree=str(repo), start_head=head,
                    members=members, scope=scope, scope_hash=scope_digest(scope), delivery_boundary="final-pr",
                    terminal_outcome="merged", confirm_schema3_cutover=True,
                ),
                "register_batch",
            )
            for member in members:
                await self.create(
                    "alpha", member,
                    batch={"batch_id": BATCH, "member_id": member, "implementation_dependencies": []},
                )
            return f"registered {BATCH} with {len(members)} members"

        async def claim_slots() -> str:
            await self.create("alpha", "gate-o1")
            await self.create("alpha", "gate-o2")
            require_ok(await call(alpha, "take", id="gate-m1"), "take of a batch member")
            require_ok(await call(alpha, "take", id="gate-o1"), "take of an ordinary item beside a held member")
            second_ordinary = require_refused(await call(alpha, "take", id="gate-o2"), "second ordinary item",
                                              "E_MULTIPLE_CLAIMS")
            second_member = require_refused(await call(alpha, "take", id="gate-m2"), "second batch member",
                                            "E_MULTIPLE_CLAIMS")
            return f"member and ordinary item held together; second ordinary {second_ordinary}, second member {second_member}"

        await report.check("register_batch with confirm_schema3_cutover", register)
        await report.check("per-batch claim slot", claim_slots)

        async def offline() -> None:
            away = self.root / "away.git"
            self.remote.rename(away)
            try:

                async def stale_read() -> str:
                    data = require_ok(await call(alpha, "list_items", limit=100), "offline list_items")
                    expect(data.get("stale") is True, f"offline read not marked stale: {sorted(data)}")
                    return f"stale read of cached rev {data.get('cached_rev', '')[:12]}"

                async def refused_write() -> str:
                    code = require_refused(await call(alpha, "create_item", id="gate-offline", title="offline"),
                                           "offline write", "E_OFFLINE")
                    return f"refused with {code}"

                await report.check("offline read served stale", stale_read)
                await report.check("offline write refused E_OFFLINE", refused_write)
            finally:
                away.rename(self.remote)

        await offline()

        async def final_validate() -> str:
            out = self.cli("validate")
            expect(out.returncode == 0, f"exit {out.returncode}: {out.stderr}")
            counts = json.loads(out.stdout)
            expect(counts["items"] == len(self.created), f"{counts['items']} items, created {len(self.created)}")
            by_status = counts["by_status"]
            expect(by_status.get("done") == self.done and by_status.get("dropped") == self.dropped,
                   f"by_status {by_status}, wanted done={self.done} dropped={self.dropped}")
            return f"items={counts['items']} by_status={by_status} live_claims={counts['live_claims']}"

        async def authorship() -> str:
            out = run(
                ["git", "-C", str(self.remote), "log", "todo-state", "--format=%an <%ae>%x09%s"], self.env, self.root,
            )
            expect(out.returncode == 0, f"git log failed: {out.stderr}")
            lines = [line for line in out.stdout.splitlines() if line]
            expect(lines, "state branch has no commits")
            bad = [line for line in lines if not (line.startswith(STATE_AUTHOR + "\t") and "\ttodo(" in line)]
            expect(not bad, f"{len(bad)} of {len(lines)} commits not authored {STATE_AUTHOR} with a todo( subject: {bad[:1]}")
            return f"{len(lines)} commits, all {STATE_AUTHOR} with todo( subjects"

        await report.check("final validate counts", final_validate)
        await report.check("state commit authorship and subjects", authorship)

    async def close(self) -> None:
        await self.stack.aclose()


async def amain(bin_dir: Path, version: str) -> int:
    root = Path(tempfile.mkdtemp(prefix="tractable-gate1-")).resolve()
    gate = Gate(bin_dir, version, root)
    try:
        try:
            await gate.run_all()
        finally:
            try:
                await gate.close()
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                print(f"note: closing servers: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    report = gate.report
    print(f"Gate 1: {report.passed}/{report.total} checks passed", flush=True)
    return 0 if report.total and report.passed == report.total else 1


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: python scripts/release_gate1.py <bin-dir> <expected-version>")
    sys.exit(anyio.run(amain, Path(sys.argv[1]).resolve(), sys.argv[2]))


if __name__ == "__main__":
    main()
