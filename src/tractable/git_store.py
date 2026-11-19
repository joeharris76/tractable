"""Persistent Git object store behind reads, writes and recovery.

Each state branch gets one bare repository under the cache directory
(``<cache>/<namespace>/store.git``). Fetches are incremental, so a new tip
costs only the objects it adds, and snapshots are read straight from Git
objects instead of being unpacked into files. Git's packed deltas keep the
steady-state cost of a commit well under the bytes of the items it
touches; maintenance (:meth:`Store.maintain`) restores that packing once
unpacked objects exceed :data:`SLACK_CAP_BYTES`.

Many processes share one store. The design relies only on what Git
already makes safe for concurrent use: objects are written atomically and
are immutable, fetches land on a per-call ref under ``refs/tractable/in/``
(no configured remote, so nothing races on remote-tracking refs), and the
one shared ref, :data:`TIP_REF`, moves only by compare-and-swap. A shared
``flock`` guards ordinary use so that a rebuild of a damaged store
(:meth:`Store.repair`) never pulls a repository out from under a reader.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .errors import E_STATE, TodoError
from .store import INDEX_FILENAME, ITEMS_DIRNAME, Snapshot, assemble_snapshot, serialize_state

try:  # Non-POSIX platforms are unsupported: without fcntl nothing is locked.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

STORE_DIRNAME = "store.git"
TIP_REF = "refs/tractable/tip"
INCOMING_PREFIX = "refs/tractable/in/"
STORE_ID_FILE = "tractable-id"
LOCK_NAME = "store.lock"
MAINTENANCE_LOCK_NAME = "maintenance.lock"
MAINTENANCE_STAMP_NAME = "maintenance.stamp"
MAINTENANCE_ERROR_NAME = "maintenance.error"
SLACK_CAP_BYTES = 8 * 1024 * 1024
# Past this, maintenance runs inline whatever the cooldown: bursts and a
# failing background run cannot grow the store without bound.
HARD_SLACK_CAP_BYTES = 2 * SLACK_CAP_BYTES
MAINTENANCE_INTERVAL_S = 600
MAINTENANCE_RETRY_S = 60
# Longest a read or write waits for the exclusive maintenance lock before it
# fails with nothing changed. A client times out at 30 s; waiting past that
# only leaves an operation of unknown outcome.
LOCK_WAIT_S = 20.0
STALE_STAGING_S = 3600
MIN_GIT_VERSION = (2, 31)
PRUNE_EXPIRE = "1.hour.ago"
STALE_INCOMING_S = 3600
MAINTENANCE_ENV = "TRACTABLE_STORE_MAINTENANCE"  # background (default) | inline | off

COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "tractable",
    "GIT_AUTHOR_EMAIL": "tractable@localhost",
    "GIT_COMMITTER_NAME": "tractable",
    "GIT_COMMITTER_EMAIL": "tractable@localhost",
}

# Store-local settings win over the user's global config: no automatic gc
# racing ours, fetched packs kept as packs, and no hooks or signing.
STORE_CONFIG = (
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("transfer.unpackLimit", "1"),
    ("fetch.writeCommitGraph", "false"),
    ("core.hooksPath", os.devnull),
    ("core.logAllRefUpdates", "false"),
    ("commit.gpgSign", "false"),
)

# Settings re-asserted on every command through GIT_CONFIG_COUNT (git 2.31+,
# the minimum version, so even `git init` cannot run a global hook), which
# outranks every config file. Inherited command-level entries stay (they
# may carry credentials) but come first, so these win for each key. Git
# drops GIT_CONFIG_COUNT for a local-path remote's receive-pack, so the
# remote's own hooks still run.
PROTECTED_CONFIG = (
    ("core.hooksPath", os.devnull),
    ("commit.gpgSign", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)

# Inherited variables that would redirect a command to another repository,
# index or identity. GIT_CONFIG_PARAMETERS (`git -c` from a parent git
# process) outranks GIT_CONFIG_COUNT, so it is dropped as well. Transport
# and credential settings pass through.
SCRUBBED_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_INDEX_VERSION",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
    "GIT_NAMESPACE", "GIT_PREFIX", "GIT_QUARANTINE_PATH", "GIT_SHALLOW_FILE",
    "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_CONFIG_PARAMETERS",
})

SYMLINK_MODE = "120000"
FILE_MODE = "100644"

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for key, value in PROTECTED_CONFIG:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    if extra:
        env.update(extra)
    return env


_GIT_VERSION: tuple[int, ...] | None = None


def git_version() -> tuple[int, ...]:
    """The installed git's version, probed once per process."""
    global _GIT_VERSION
    if _GIT_VERSION is None:
        try:
            out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        numbers = []
        for part in (out.split()[2:3] or ["0"])[0].split("."):
            if not part.isdigit():
                break
            numbers.append(int(part))
        _GIT_VERSION = tuple(numbers) or (0,)
    return _GIT_VERSION


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 60,
    *,
    input: str | bytes | None = None,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    # Without input, git gets a null stdin: under the MCP server, stdin is the
    # JSON-RPC stream, and no git command may read from it.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        input=input,
        stdin=subprocess.DEVNULL if input is None else None,
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
        env=git_env(env),
    )


def cleanup_message(message: str) -> str:
    """Normalize a commit message the way ``git commit -m`` does.

    That is ``--cleanup=whitespace``: strip trailing whitespace from each
    line, drop leading and trailing blank lines, collapse runs of blank
    lines, and end with exactly one newline. ``commit-tree`` stores its
    input verbatim, so plumbing-built commits apply this first to keep
    history identical to the porcelain path.
    """
    out: list[str] = []
    pending_blank = False
    for line in message.split("\n"):
        line = line.rstrip(" \t\r\v\f")
        if not line:
            pending_blank = bool(out)
            continue
        if pending_blank:
            out.append("")
            pending_blank = False
        out.append(line)
    return "\n".join(out) + "\n" if out else ""


class StoreError(Exception):
    """A local store command failed; the store may be damaged."""


class StoreBusy(TodoError):
    """The store stayed locked by maintenance; the caller changed nothing."""


def _brief(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).strip().split())
    return text if len(text) <= limit else text[:limit] + "…"


Entry = tuple[str, str, str]  # (mode, type, object id)


class Store:
    """One state branch's object store. See the module docstring."""

    def __init__(self, ns: Path, run: Runner, timeout: int = 60) -> None:
        self.ns = Path(ns)
        self.git_dir = self.ns / STORE_DIRNAME
        self._run = run
        self.timeout = timeout
        # The store ID seen when the current operation took its lock: a
        # repair moves aside only that store, never a newer replacement.
        self.active_id = ""

    # -- plumbing --

    def git(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        input: str | bytes | None = None,
        env: dict[str, str] | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = input
        if env:
            kwargs["env"] = env
        if not text:
            kwargs["text"] = False
        return self._run(args, self.git_dir, timeout or self.timeout, **kwargs)

    def _ok(self, args: list[str], **kwargs: Any) -> Any:
        proc = self.git(args, **kwargs)
        if proc.returncode != 0:
            stderr = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
            raise StoreError(f"git {args[0]} failed in {self.git_dir}: {_brief(stderr)}")
        return proc.stdout

    # -- lifecycle --

    def exists(self) -> bool:
        return (self.git_dir / "HEAD").is_file()

    def open(self) -> None:
        """Create the store if needed; safe when processes race to do it."""
        if self.exists():
            return
        if git_version() < MIN_GIT_VERSION:
            wanted = ".".join(map(str, MIN_GIT_VERSION))
            raise StoreError(f"git {wanted} or newer is required; found {'.'.join(map(str, git_version()))}")
        self.ns.mkdir(parents=True, exist_ok=True)
        staging = self.ns / f".init-{uuid4().hex}"
        try:
            init = self._run(["init", "--quiet", "--bare", str(staging)], self.ns, self.timeout)
            if init.returncode != 0:
                raise StoreError(f"git init failed in {self.ns}: {_brief(init.stderr)}")
            # One append instead of a `git config` call per key: store
            # creation sits on the first read's latency.
            with open(staging / "config", "a", encoding="utf-8") as handle:
                for key, value in STORE_CONFIG:
                    section, name = key.split(".")
                    handle.write(f"[{section}]\n\t{name} = {value}\n")
            (staging / STORE_ID_FILE).write_text(uuid4().hex + "\n", encoding="utf-8")
            try:
                os.rename(staging, self.git_dir)
            except OSError:
                if not self.exists():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def store_id(self) -> str:
        try:
            return (self.git_dir / STORE_ID_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @contextmanager
    def _flock(
        self, name: str, mode: int, blocking: bool = True, wait_s: float | None = None,
    ) -> Iterator[bool]:
        """Lock ``<ns>/<name>``, retrying if the file was replaced meanwhile.

        `cache prune` renames a whole namespace away while holding its
        lock. A process that was waiting then holds a lock on the moved
        file, so it reopens the path and locks again. With *wait_s*, a
        blocking lock gives up after that many seconds with :class:`StoreBusy`.
        """
        if fcntl is None:
            yield True
            return
        path = self.ns / name
        while True:
            self.ns.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if blocking and wait_s is not None:
                    self._lock_within(fd, mode, wait_s)
                else:
                    fcntl.flock(fd, mode | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                os.close(fd)
                yield False
                return
            except StoreBusy:
                os.close(fd)
                raise
            try:
                current = os.stat(path).st_ino == os.fstat(fd).st_ino
            except OSError:
                current = False
            if current:
                break
            os.close(fd)
        try:
            yield True
        finally:
            os.close(fd)

    @staticmethod
    def _lock_within(fd: int, mode: int, wait_s: float) -> None:
        deadline = time.monotonic() + wait_s
        delay = 0.01
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreBusy(
                        f"the local state cache stayed locked for {wait_s:g}s (maintenance is repacking it); "
                        "nothing was changed",
                        code=E_STATE,
                        recovery=["retry the call"],
                    ) from None
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    @contextmanager
    def using(self) -> Iterator[None]:
        """Hold the shared lock for ordinary reads and writes.

        The store is created under the lock if it is missing, so a holder
        never sees a namespace without a repository. Waits at most
        :data:`LOCK_WAIT_S` for maintenance to release it.
        """
        with self._flock(LOCK_NAME, fcntl.LOCK_SH if fcntl else 0, wait_s=LOCK_WAIT_S):
            # The lock file's mtime records last use, for `cache prune`.
            try:
                os.utime(self.ns / LOCK_NAME)
            except OSError:
                pass
            self.open()
            self.active_id = self.store_id()
            yield

    def repair(self, failed_id: str) -> None:
        """Replace a damaged store with a fresh, empty one.

        Runs under the exclusive lock, so no reader holds the store while
        it is moved aside and recreated. Only the store that failed is
        replaced: if another process already rebuilt it (a different store
        ID), this does nothing.
        """
        trash = None
        with self._flock(LOCK_NAME, fcntl.LOCK_EX if fcntl else 0):
            if self.exists() and self.store_id() == failed_id:
                trash = self.ns / f".trash-{uuid4().hex}"
                os.rename(self.git_dir, trash)
            self.open()
        if trash is not None:
            shutil.rmtree(trash, ignore_errors=True)

    # -- refs --

    def resolve(self, rev: str) -> str | None:
        proc = self.git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
        return proc.stdout.strip() if proc.returncode == 0 else None

    def has_commit(self, rev: str) -> bool:
        return self.git(["cat-file", "-e", f"{rev}^{{commit}}"]).returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.git(["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0

    def tip(self) -> str | None:
        # The store never packs refs, so the loose ref file is the ref; read
        # it directly to save a subprocess on every read.
        try:
            value = (self.git_dir / TIP_REF).read_text(encoding="ascii").strip()
        except OSError:
            return None
        return value if value and all(c in "0123456789abcdef" for c in value) else self.resolve(TIP_REF)

    def advance_tip(self, new: str) -> None:
        """Point :data:`TIP_REF` at *new* unless it already holds a descendant.

        The branch is append-only, so a newer tip descends from an older
        one and the ref never moves backwards when processes race. A tip
        that does not descend (a rewritten remote branch) still replaces
        it: the remote is authoritative. Best effort: a lost race retries.
        """
        for _ in range(5):
            current = self.tip()
            if current == new or (current and self.is_ancestor(new, current)):
                return
            old = current or "0" * len(new)
            if self.git(["update-ref", TIP_REF, new, old]).returncode == 0:
                return

    @contextmanager
    def incoming(
        self, url: str, branch: str, timeout: int | None = None,
    ) -> Iterator[tuple[subprocess.CompletedProcess[Any], str | None]]:
        """Fetch the branch onto a private ref; yield (process, fetched tip).

        Fetching by URL with an explicit refspec updates nothing but that
        ref, and every local ref is advertised as a "have", so only new
        objects transfer. The ref is deleted on exit; callers keep what
        they need reachable through :meth:`advance_tip`.
        """
        ref = INCOMING_PREFIX + uuid4().hex
        # FETCH_HEAD is never read; skipping it also keeps concurrent
        # fetches from interleaving writes to it.
        try:
            proc = self.git(
                ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", url, f"+refs/heads/{branch}:{ref}"],
                timeout=timeout or self.timeout,
            )
            yield proc, (self.resolve(ref) if proc.returncode == 0 else None)
        finally:
            self.git(["update-ref", "-d", ref])

    # -- reading --

    def entries(self, rev: str) -> dict[str, Entry]:
        raw = self._ok(["ls-tree", "-r", "-t", "-z", "--full-tree", rev], text=False)
        out: dict[str, Entry] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, oid = meta.decode("ascii").split()
            out[path.decode("utf-8", "surrogateescape")] = (mode, kind, oid)
        return out

    def blobs(self, oids: set[str]) -> dict[str, bytes]:
        if not oids:
            return {}
        ordered = sorted(oids)
        raw = self._ok(["cat-file", "--batch"], input=("\n".join(ordered) + "\n").encode("ascii"), text=False)
        out: dict[str, bytes] = {}
        pos = 0
        for oid in ordered:
            end = raw.index(b"\n", pos)
            header = raw[pos:end].decode("ascii").split()
            if len(header) != 3:
                raise StoreError(f"object {oid} is missing from {self.git_dir}")
            size = int(header[2])
            out[oid] = raw[end + 1 : end + 1 + size]
            pos = end + 1 + size + 1
        return out

    def blob(self, oid: str) -> bytes:
        return self.blobs({oid})[oid]

    def load(self, rev: str) -> tuple[Snapshot, dict[str, tuple[str, str, bytes]]]:
        """Load *rev* exactly as :func:`store.load_snapshot` loads a checkout.

        Returns the snapshot plus the state files it was built from, as
        ``{path: (mode, object id, bytes)}``, so a writer can tell which
        files its change actually touches.
        """
        entries = self.entries(rev)
        where = f"revision {rev[:12]}"
        items_entry = entries.get(ITEMS_DIRNAME)
        items_linked = items_entry is not None and items_entry[0] == SYMLINK_MODE

        def is_state_file(path: str) -> bool:
            if path == INDEX_FILENAME:
                return True
            head, _, name = path.partition("/")
            return head == ITEMS_DIRNAME and "/" not in name and name.endswith(".json")

        wanted = {
            path: entry for path, entry in entries.items()
            if is_state_file(path) and entry[1] == "blob" and entry[0] != SYMLINK_MODE
        }
        data = self.blobs({oid for _, _, oid in wanted.values()})

        def read_file(rel: str, label: str) -> Any:
            if rel.startswith(f"{ITEMS_DIRNAME}/") and items_linked:
                raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
            entry = entries.get(rel)
            if entry is None:
                raise TodoError(f"missing {label} in {where}: is this a state checkout?", code=E_STATE)
            if entry[0] == SYMLINK_MODE:
                raise TodoError(f"refusing to follow symlink at {label}", code=E_STATE)
            if rel not in wanted:
                raise TodoError(f"{label} in {where} is not a regular file", code=E_STATE)
            try:
                return json.loads(data[entry[2]].decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise TodoError(f"invalid {label}: {exc}", code=E_STATE) from exc

        def detail_names() -> list[str]:
            if items_linked:
                raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
            return [path.split("/", 1)[1] for path in entries if path != INDEX_FILENAME and is_state_file(path)]

        snapshot = assemble_snapshot(read_file, detail_names)
        files = {path: (mode, oid, data[oid]) for path, (mode, _, oid) in wanted.items()}
        return snapshot, files

    # -- writing --

    def write_tree(self, parent: str, snapshot: Snapshot, files: dict[str, tuple[str, str, bytes]]) -> str:
        """Write the tree for *snapshot* on top of *parent*.

        Only state files whose bytes or mode differ are hashed, which gives
        the same tree as rewriting every file in a checkout and running
        ``git add -- index.json items``: canonical bytes, mode 100644, and
        detail files that left the snapshot removed.
        """
        wanted: dict[str, bytes] = {INDEX_FILENAME: serialize_state(snapshot.index).encode("utf-8")}
        for item_id, detail in snapshot.details.items():
            wanted[f"{ITEMS_DIRNAME}/{item_id}.json"] = serialize_state(detail).encode("utf-8")
        changed = [
            path for path, body in wanted.items()
            if path not in files or files[path][2] != body or files[path][0] != FILE_MODE
        ]
        removed = [path for path in files if path not in wanted]
        lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="todo-tree-") as tmp:
            if changed:
                paths = []
                for n, path in enumerate(changed):
                    target = Path(tmp) / f"{n}.blob"
                    target.write_bytes(wanted[path])
                    paths.append(str(target))
                oids = self._ok(
                    ["hash-object", "-w", "--no-filters", "--stdin-paths"], input="\n".join(paths) + "\n",
                ).split()
                if len(oids) != len(changed):
                    raise StoreError("hash-object returned the wrong number of objects")
                lines += [f"{FILE_MODE} {oid}\t{path}" for oid, path in zip(oids, changed)]
            zero = "0" * len(parent)
            lines += [f"0 {zero}\t{path}" for path in removed]
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._ok(["read-tree", parent], env=env)
            if lines:
                self._ok(["update-index", "--index-info"], input="\n".join(lines) + "\n", env=env)
            return self._ok(["write-tree"], env=env).strip()

    def tree_of(self, rev: str) -> str:
        return self._ok(["rev-parse", f"{rev}^{{tree}}"]).strip()

    def commit(self, tree: str, parent: str, message: str) -> str:
        return self._ok(
            ["commit-tree", tree, "-p", parent], input=cleanup_message(message), env=COMMIT_IDENTITY,
        ).strip()

    def push(self, url: str, sha: str, branch: str) -> subprocess.CompletedProcess[Any]:
        return self.git(["push", "--quiet", url, f"{sha}:refs/heads/{branch}"])

    # -- maintenance --

    def slack(self) -> int:
        """Bytes in loose objects and in every pack but the largest.

        Pack indexes and other metadata are not counted: they shrink with
        the packs they describe, and counting them would push a large store
        past the cap permanently. Measured from the object directory
        without running git, because it is checked after every fetch and
        write.
        """
        objects = self.git_dir / "objects"
        packs: list[int] = []
        loose = 0
        try:
            with os.scandir(objects) as top:
                for entry in top:
                    if entry.name == "pack":
                        with os.scandir(entry.path) as pack_dir:
                            for item in pack_dir:
                                if item.name.endswith(".pack"):
                                    packs.append(item.stat().st_size)
                    elif len(entry.name) == 2 and entry.is_dir():
                        with os.scandir(entry.path) as fanout:
                            loose += sum(item.stat().st_size for item in fanout if item.is_file())
        except OSError:
            return 0
        return max(0, loose + sum(packs) - max(packs, default=0))

    def maybe_maintain(self) -> None:
        """Start maintenance when slack passes the cap. Never raises.

        Below :data:`HARD_SLACK_CAP_BYTES` a detached process runs it, at most
        once per :data:`MAINTENANCE_INTERVAL_S` (or :data:`MAINTENANCE_RETRY_S`
        after a failure). Past the hard cap it runs inline, so bursts of
        writes and a failing background run are bounded and the caller pays.
        """
        mode = os.environ.get(MAINTENANCE_ENV, "background")
        if mode == "off":
            return
        try:
            slack = self.slack()
            if slack <= SLACK_CAP_BYTES:
                return
            if slack <= HARD_SLACK_CAP_BYTES:
                stamp = self.ns / MAINTENANCE_STAMP_NAME
                failed = (self.ns / MAINTENANCE_ERROR_NAME).exists()
                interval = MAINTENANCE_RETRY_S if failed else MAINTENANCE_INTERVAL_S
                if stamp.exists() and time.time() - stamp.stat().st_mtime < interval:
                    return
            if mode == "inline" or slack > HARD_SLACK_CAP_BYTES:
                self.maintain()
                return
            env = git_env()
            # The child must import this package however the parent did.
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            subprocess.Popen(
                [sys.executable, "-c", MAINTAIN_ENTRY, "maintain", str(self.ns)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, env=env,
            )
        except (OSError, StoreError, subprocess.SubprocessError):
            return

    def maintain(self) -> bool:
        """Repack, prune and drop leaked fetch refs. False when already running.

        ``repack -A`` keeps unreachable objects as loose objects stamped
        with their pack's time, and ``prune`` removes only those older than
        :data:`PRUNE_EXPIRE`. Objects another process wrote moments ago and
        has not referenced yet (a fetch about to set its ref, a commit about
        to be pushed) therefore survive. Packs created after the repack
        starts are never touched. The shared store lock is held throughout,
        so a repair never moves the store aside mid-repack. A failure is
        recorded in ``maintenance.error`` and retried sooner.
        """
        if not self.exists():
            return False
        with self._flock(MAINTENANCE_LOCK_NAME, fcntl.LOCK_EX if fcntl else 0, blocking=False) as got:
            if not got:
                return False
            with self._flock(LOCK_NAME, fcntl.LOCK_EX if fcntl else 0):
                if not self.exists():
                    return False
                (self.ns / MAINTENANCE_STAMP_NAME).touch()
                error = self.ns / MAINTENANCE_ERROR_NAME
                try:
                    cutoff = time.time() - STALE_INCOMING_S
                    incoming = self.git_dir / INCOMING_PREFIX.rstrip("/")
                    for ref in incoming.glob("*") if incoming.is_dir() else []:
                        try:
                            if ref.stat().st_mtime < cutoff:
                                self.git(["update-ref", "-d", INCOMING_PREFIX + ref.name])
                        except OSError:
                            continue
                    self._ok(["repack", "-A", "-d", "-l", "-q"], timeout=max(self.timeout, 600))
                    self._ok(["prune", f"--expire={PRUNE_EXPIRE}"], timeout=max(self.timeout, 600))
                except (OSError, StoreError, subprocess.SubprocessError) as exc:
                    try:
                        error.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {exc}\n")
                    except OSError:
                        pass
                    raise
                try:
                    error.unlink()
                except FileNotFoundError:
                    pass
                return True


LEGACY_NAMES = ("revs", "last")
DEFAULT_PRUNE_DAYS = 30.0
MIN_PRUNE_DAYS = 1.0


def _is_hex(name: str, length: int) -> bool:
    return len(name) == length and all(c in "0123456789abcdef" for c in name)


def _last_used(ns: Path) -> float:
    stamps = [ns / LOCK_NAME, ns / "last", ns / "revs", ns / STORE_DIRNAME]
    times = []
    for path in stamps:
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    if not times:
        try:
            return ns.stat().st_mtime
        except OSError:
            return time.time()
    return max(times)


@contextmanager
def _probe_unlocked(lock: Path) -> Iterator[bool]:
    """For a dry run: is the namespace free, without creating its lock file?"""
    if fcntl is None or not lock.exists():
        yield True
        return
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        fcntl.flock(fd, fcntl.LOCK_UN)
        yield True
    finally:
        os.close(fd)


def _is_legacy(name: str) -> bool:
    return name in LEGACY_NAMES or (name.startswith(".last.") and name.endswith(".tmp"))


def prune_cache(
    cache_dir: Path,
    *,
    older_than_days: float = DEFAULT_PRUNE_DAYS,
    dry_run: bool = False,
    run: Runner = run_git,
    now: float | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Remove what the object store made obsolete, then maintain each store.

    Removed: snapshot folders from before namespacing (40-hex names),
    leftover trash, namespaces unused for *older_than_days* (at least
    :data:`MIN_PRUNE_DAYS`), abandoned store staging, and, in a namespace
    whose store already has a tip, the legacy ``revs/``, ``last`` and
    ``.last.*.tmp`` files. *aliases* maps a legacy namespace name to the
    store namespace that replaced it (a local remote whose resolved path
    hashes differently); the legacy files go once that store has a tip.
    A namespace with no store tip otherwise keeps its legacy revision,
    which is still its only offline copy.

    Each namespace is handled under its exclusive lock, taken without
    waiting: one in use is skipped, and eligibility is checked again
    under the lock. Victims are renamed into a trash folder first, so a
    reader never sees a half-deleted entry, and then deleted.
    """
    cache_dir = Path(cache_dir)
    older_than_days = max(float(older_than_days), MIN_PRUNE_DAYS)
    report: dict[str, Any] = {
        "cache_dir": str(cache_dir), "dry_run": dry_run, "removed": [], "maintained": [], "skipped_busy": [],
    }
    if not cache_dir.is_dir():
        return report
    clock = now if now is not None else time.time()
    cutoff = clock - older_than_days * 86400
    aliases = aliases or {}
    trash = cache_dir / f".trash-{uuid4().hex}"
    removed: list[str] = []
    stores: list[Store] = []

    def discard(victim: Path) -> None:
        removed.append(str(victim))
        if dry_run:
            return
        trash.mkdir(exist_ok=True)
        try:
            os.rename(victim, trash / f"{len(removed)}-{victim.name}")
        except OSError:
            removed.pop()

    for entry in sorted(cache_dir.iterdir()):
        if entry == trash or not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".trash-"):
            # A younger trash folder may belong to a prune still deleting it.
            try:
                abandoned = clock - entry.stat().st_mtime > STALE_STAGING_S
            except OSError:
                abandoned = False
            if abandoned:
                discard(entry)
            continue
        if _is_hex(entry.name, 40):
            discard(entry)
            continue
        if not _is_hex(entry.name, 16):
            continue
        store = Store(entry, run)
        lock = entry / LOCK_NAME
        lock_existed = lock.exists()
        locking = _probe_unlocked(lock) if dry_run else store._flock(
            LOCK_NAME, fcntl.LOCK_EX if fcntl else 0, blocking=False,
        )
        with locking as got:
            if not got:
                report["skipped_busy"].append(str(entry))
                continue
            if not lock_existed and not dry_run:
                # Creating the lock is not use: keep its fresh mtime from
                # making a stale namespace look current.
                try:
                    os.utime(lock, (0, 0))
                except OSError:
                    pass
            if _last_used(entry) < cutoff:
                discard(entry)
                continue
            owner = Store(cache_dir / aliases[entry.name], run) if entry.name in aliases else store
            has_tip = owner.tip() is not None
            for child in sorted(entry.iterdir()):
                staging = child.name.startswith((".trash-", ".init-"))
                if staging:
                    try:
                        staging = clock - child.stat().st_mtime > STALE_STAGING_S
                    except OSError:
                        staging = False
                if staging or (_is_legacy(child.name) and has_tip):
                    discard(child)
        if store.exists():
            stores.append(store)
    report["removed"] = removed
    if dry_run:
        report["maintained"] = [str(s.ns) for s in stores]
        return report
    shutil.rmtree(trash, ignore_errors=True)
    for store in stores:
        try:
            if store.maintain():
                report["maintained"].append(str(store.ns))
        except (OSError, StoreError, subprocess.SubprocessError):
            continue
    return report


def _main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "maintain":
        try:
            Store(Path(argv[1]), run_git).maintain()
        except (OSError, StoreError, subprocess.SubprocessError):
            return 1
        return 0
    return 2


# Detached maintenance runs through -c: `python -m` would re-import this
# module, which the package has already imported.
MAINTAIN_ENTRY = "import sys; from tractable.git_store import _main; sys.exit(_main(sys.argv[1:]))"
