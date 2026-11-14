"""Integrity checks for the release's index distribution gate."""

from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest


_verifier = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "verify_index_release.py"))
verify_artifacts = _verifier["verify_artifacts"]
isolated_env = _verifier["isolated_env"]
wait_for_index_listing = _verifier["wait_for_index_listing"]
install_after_listing = _verifier["install_after_listing"]


def test_smoke_environment_ignores_caller_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "TRACTABLE_STATE_BRANCH": "wrong", "GIT_CONFIG_COUNT": "1", "UV_INDEX": "https://example.invalid",
        "PIP_INDEX_URL": "https://example.invalid", "PYTHONPATH": "/elsewhere", "XDG_CONFIG_HOME": "/elsewhere",
    }.items():
        monkeypatch.setenv(key, value)
    env = isolated_env(tmp_path)
    for key in ("TRACTABLE_STATE_BRANCH", "GIT_CONFIG_COUNT", "UV_INDEX", "PIP_INDEX_URL", "PYTHONPATH"):
        assert key not in env
    assert env["HOME"] == str(tmp_path)
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / ".config")
    assert env["PATH"] == os.environ["PATH"]


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, dict]:
    dist = tmp_path / "dist"
    dist.mkdir()
    index = tmp_path / "index"
    index.mkdir()
    urls = []
    for filename in ("tractable-0.14.0-py3-none-any.whl", "tractable-0.14.0.tar.gz"):
        content = filename.encode("utf-8")
        (dist / filename).write_bytes(content)
        published = index / filename
        published.write_bytes(content)
        urls.append({
            "filename": filename, "url": published.as_uri(), "yanked": False,
            "digests": {"sha256": hashlib.sha256(content).hexdigest()},
        })
    return dist, {"info": {"version": "0.14.0"}, "urls": urls}


def test_matching_release_bytes_pass(release: tuple[Path, dict]) -> None:
    dist, metadata = release
    verify_artifacts(dist, "0.14.0", metadata)


@pytest.mark.parametrize("location", ["build", "index-metadata", "download"])
def test_altered_distribution_is_rejected(release: tuple[Path, dict], location: str) -> None:
    dist, metadata = release
    asset = metadata["urls"][0]
    if location == "build":
        (dist / asset["filename"]).write_bytes(b"different build")
    elif location == "index-metadata":
        asset["digests"]["sha256"] = "0" * 64
    else:
        (dist.parent / "index" / asset["filename"]).write_bytes(b"different download")
    with pytest.raises(ValueError, match="differs|differ"):
        verify_artifacts(dist, "0.14.0", metadata)


@pytest.mark.parametrize("mutation", ["missing-wheel", "missing-sdist", "duplicate", "wrong-version", "yanked"])
def test_incomplete_or_unusable_release_is_rejected(release: tuple[Path, dict], mutation: str) -> None:
    dist, metadata = release
    if mutation == "missing-wheel":
        (dist / metadata["urls"][0]["filename"]).unlink()
    elif mutation == "missing-sdist":
        metadata["urls"].pop()
    elif mutation == "duplicate":
        metadata["urls"].append(metadata["urls"][0])
    elif mutation == "wrong-version":
        metadata["info"]["version"] = "0.13.0"
    else:
        metadata["urls"][0]["yanked"] = True
    with pytest.raises(ValueError):
        verify_artifacts(dist, "0.14.0", metadata)


VERSION = "1.2.0"


def _listing(*versions: str) -> bytes:
    files = [{"filename": f"tractable-{v}-py3-none-any.whl"} for v in versions]
    return json.dumps({"meta": {"api-version": "1.0"}, "name": "tractable", "files": files}).encode()


class FakeIndex:
    """Serves a listing that gains the version after ``after`` polls; time only moves when slept."""

    def __init__(self, after: int | None, *, errors: list[int] | None = None, body: bytes | None = None) -> None:
        self.after = after
        self.errors = list(errors or [])
        self.body = body
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.now = 0.0
        self.sleeps: list[float] = []

    def fetch(self, url: str, headers: dict[str, str]) -> bytes:
        self.requests.append((url, headers))
        if self.errors:
            code = self.errors.pop(0)
            raise HTTPError(url, code, "error", {}, io.BytesIO())
        if self.body is not None:
            return self.body
        listed = self.after is not None and len(self.requests) > self.after
        return _listing("1.1.1", VERSION) if listed else _listing("1.1.1")

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def wait(self, deadline: float = 480.0, interval: float = 15.0) -> None:
        wait_for_index_listing(
            VERSION, fetch=self.fetch, sleep=self.sleep, clock=lambda: self.now,
            interval=interval, deadline=deadline,
        )


def test_install_runs_once_after_the_version_is_listed() -> None:
    index = FakeIndex(after=3)
    events: list[str] = []
    install_after_listing(
        VERSION, lambda: events.append(f"install after {len(index.requests)} polls"),
        fetch=index.fetch, sleep=index.sleep, clock=lambda: index.now, interval=15.0, deadline=480.0,
    )
    assert events == ["install after 4 polls"]
    assert index.sleeps == [15.0] * 3


def test_listed_version_installs_without_sleeping() -> None:
    index = FakeIndex(after=0)
    index.wait()
    assert len(index.requests) == 1 and index.sleeps == []


def test_poll_requests_bypass_the_index_cache() -> None:
    index = FakeIndex(after=1)
    index.wait()
    assert {url for url, _ in index.requests} == {"https://pypi.org/simple/tractable/"}
    for _, headers in index.requests:
        assert headers["Cache-Control"] == "no-cache"
        assert headers["Accept"] == "application/vnd.pypi.simple.v1+json"


def test_default_fetch_sends_the_cache_bypass_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen.append(request)
        return Response(_listing(VERSION))

    monkeypatch.setitem(_verifier["fetch_index"].__globals__, "urlopen", fake_urlopen)
    wait_for_index_listing(VERSION, sleep=lambda s: pytest.fail("slept"))
    assert seen[0].full_url == "https://pypi.org/simple/tractable/"
    assert seen[0].get_header("Cache-control") == "no-cache"
    assert seen[0].get_header("Accept") == "application/vnd.pypi.simple.v1+json"


def test_never_listed_fails_after_the_bounded_wait_without_installing() -> None:
    index = FakeIndex(after=None)
    installs: list[int] = []
    with pytest.raises(ValueError, match=rf"{VERSION}.*within 480s"):
        install_after_listing(
            VERSION, lambda: installs.append(1),
            fetch=index.fetch, sleep=index.sleep, clock=lambda: index.now, interval=15.0, deadline=480.0,
        )
    assert installs == []
    assert sum(index.sleeps) == 480.0 and len(index.requests) == 33


def test_install_failure_after_listing_is_not_retried() -> None:
    index = FakeIndex(after=0)
    installs: list[int] = []

    def failing_install() -> None:
        installs.append(1)
        raise subprocess.CalledProcessError(1, ["uv", "pip", "install"])

    with pytest.raises(subprocess.CalledProcessError):
        install_after_listing(
            VERSION, failing_install,
            fetch=index.fetch, sleep=index.sleep, clock=lambda: index.now, interval=15.0, deadline=480.0,
        )
    assert installs == [1] and len(index.requests) == 1


def test_transient_server_errors_are_polled_again() -> None:
    index = FakeIndex(after=0, errors=[503, 502])
    index.wait()
    assert len(index.requests) == 3 and index.sleeps == [15.0, 15.0]


def test_persistent_server_errors_fail_at_the_deadline() -> None:
    index = FakeIndex(after=0, errors=[503] * 100)
    with pytest.raises(ValueError, match=r"HTTP 503"):
        index.wait(deadline=60.0)
    assert sum(index.sleeps) == 60.0


@pytest.mark.parametrize("code", [400, 403, 404, 429])
def test_other_http_errors_fail_at_once(code: int) -> None:
    index = FakeIndex(after=0, errors=[code])
    with pytest.raises(ValueError, match=f"HTTP {code}"):
        index.wait()
    assert len(index.requests) == 1 and index.sleeps == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b"{}", b'{"files": "x"}', b'{"files": [1]}', b'{"files": [{"url": "u"}]}'],
)
def test_malformed_listing_fails_at_once(body: bytes) -> None:
    index = FakeIndex(after=0, body=body)
    with pytest.raises(ValueError, match="malformed"):
        index.wait()
    assert len(index.requests) == 1 and index.sleeps == []
