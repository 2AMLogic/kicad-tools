"""The Test job's pinned actionlint install survives a transient release-host 504 (Issue #10485).

PR #10481's first CI attempt stopped in ``Install pinned actionlint``: GitHub
Releases answered the archive download with a 504, the step made a single
attempt, and the job ended before checkout. These tests execute that step's
own ``run`` block against a loopback release host, substituting only the
download origin and the install prefix.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

if shutil.which("curl") is None or shutil.which("sha256sum") is None:
    pytest.skip("the install step needs curl and sha256sum", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
STEP_NAME = "Install pinned actionlint"
RELEASE_ORIGIN = "https://github.com"
INSTALL_PATH = "/usr/local/bin/actionlint"
VERSION = "1.7.12"
ARCHIVE_PATH = (
    f"/rhysd/actionlint/releases/download/v{VERSION}/actionlint_{VERSION}_linux_amd64.tar.gz"
)
FAKE_VERSION_OUTPUT = "fake-actionlint 10485"
PINNED_PAYLOAD = f'#!/bin/sh\necho "{FAKE_VERSION_OUTPUT}"\n'.encode()
UNPINNED_PAYLOAD = b'#!/bin/sh\necho "a build the pin does not name"\n'

_TEST_STEPS = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"][
    "test"
]["steps"]


class _Releases:
    """What the release host answers: each status once, the last one forever."""

    def __init__(self, statuses: list[int], archive: bytes) -> None:
        self.statuses = statuses
        self.archive = archive
        self.paths: list[str] = []


class _ReleaseHandler(BaseHTTPRequestHandler):
    releases: _Releases

    def do_GET(self) -> None:
        releases = self.releases
        releases.paths.append(self.path)
        status = releases.statuses[min(len(releases.paths) - 1, len(releases.statuses) - 1)]
        payload = (
            releases.archive if status == 200 else b"<html><body>Gateway Timeout</body></html>\n"
        )
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args: object) -> None:
        pass


def _archive(payload: bytes) -> bytes:
    """A stand-in for the release tarball: one executable named ``actionlint``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        member = tarfile.TarInfo("actionlint")
        member.size = len(payload)
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


def _digest(archive: bytes) -> str:
    return hashlib.sha256(archive).hexdigest()


def _install(tmp_path: Path, statuses: list[int], served: bytes, pinned: str):
    """Run the workflow step's ``run`` block against a scripted release host."""
    for directory in ("bin", "scratch", "home"):
        (tmp_path / directory).mkdir()
    step = next(s for s in _TEST_STEPS if s.get("name") == STEP_NAME)
    assert step["run"].count(RELEASE_ORIGIN) == 1, (
        "the step no longer names its download origin once"
    )
    assert step["run"].count(INSTALL_PATH) == 1, "the step no longer installs to one path"
    handler = type("Handler", (_ReleaseHandler,), {"releases": _Releases(statuses, served)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    binary = tmp_path / "bin" / "actionlint"
    script = step["run"].replace(RELEASE_ORIGIN, f"http://127.0.0.1:{server.server_port}")
    script = script.replace(INSTALL_PATH, str(binary))
    try:
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(tmp_path / "home"),
                "CURL_HOME": str(tmp_path / "home"),
                "XDG_CONFIG_HOME": str(tmp_path / "home"),
                "TMPDIR": str(tmp_path / "scratch"),
                "ACTIONLINT_VERSION": VERSION,
                "ACTIONLINT_SHA256": pinned,
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return result, handler.releases.paths, binary, tmp_path / "scratch"


def _assert_nothing_installed(binary: Path, scratch: Path) -> None:
    assert not binary.exists(), "actionlint was installed from a failed step"
    assert list(scratch.rglob("actionlint")) == [], "the archive was extracted by a failed step"


@pytest.mark.parametrize(
    "statuses, requests, installed",
    [([504, 200], 2, True), ([504], 4, False)],
    ids=["one-504-then-200", "only-504s"],
)
def test_install_step_retries_a_transient_504(tmp_path, statuses, requests, installed):
    pinned_archive = _archive(PINNED_PAYLOAD)
    result, paths, binary, scratch = _install(
        tmp_path, statuses, pinned_archive, _digest(pinned_archive)
    )
    assert paths == [ARCHIVE_PATH] * requests, result.stdout + result.stderr
    if not installed:
        assert result.returncode != 0, result.stdout + result.stderr
        _assert_nothing_installed(binary, scratch)
        return
    assert result.returncode == 0, result.stdout + result.stderr
    downloaded = list(scratch.rglob("actionlint_*.tar.gz"))
    assert [path.read_bytes() for path in downloaded] == [pinned_archive]
    assert FAKE_VERSION_OUTPUT in result.stdout
    probe = subprocess.run([str(binary), "-version"], capture_output=True, text=True)
    assert probe.stdout.strip() == FAKE_VERSION_OUTPUT


def test_install_step_rejects_a_200_with_the_wrong_digest(tmp_path):
    pinned_archive = _archive(PINNED_PAYLOAD)
    served_archive = _archive(UNPINNED_PAYLOAD)
    result, paths, binary, scratch = _install(
        tmp_path, [200], served_archive, _digest(pinned_archive)
    )
    assert paths == [ARCHIVE_PATH]
    assert result.returncode != 0, result.stdout + result.stderr
    downloaded = list(scratch.rglob("actionlint_*.tar.gz"))
    assert [path.read_bytes() for path in downloaded] == [served_archive]
    assert "FAILED" in result.stdout
    _assert_nothing_installed(binary, scratch)
