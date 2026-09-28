"""Exercise the real CLI and Dropbox SDK over HTTP against a local test server."""

import json
import os
import signal
import subprocess
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import parse_qs

import pytest

from tests.test_integrity import reference_hash

# Only the HTTP destination is redirected. CLI, OAuth, SDK serialization,
# scanner, filters, concurrency, streaming, verification and exit codes are real.
RUNNER = """
import os, runpy
from urllib.parse import urlsplit
from requests.sessions import Session
original_send = Session.send

def local_send(self, request, **kwargs):
    url = urlsplit(request.url)
    if url.hostname not in ('api.dropboxapi.com', 'content.dropboxapi.com'):
        raise AssertionError('Unexpected request host')
    request.url = os.environ['TEST_SERVER'] + url.path
    kwargs['proxies'] = {}
    return original_send(self, request, **kwargs)

Session.send = local_send
runpy.run_module('dbxpull', run_name='__main__')
"""


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.mode = "normal"
        self.calls = Counter()
        self.downloads = Counter()
        self.lock = Lock()
        self.errors = []
        self.transfer_started = Event()
        self.release_transfer = Event()
        self.data = {
            "/docs/hello.txt": b"hello Dropbox",
            "/empty": b"",
            "/large.bin": bytes(range(256)) * (16 * 1024 + 1),
            "/data": b"data",
            "/data.part": b"real file with a part suffix",
            "/project/node_modules/skip.js": b"filtered",
            "/project/DerivedData/cache": b"filtered",
            "/project/Pods/cache": b"filtered",
            "/project/.env": b"test=true",
        }
        self.metadata = []
        for i, (path, data) in enumerate(self.data.items(), 1):
            self.metadata.append({
                ".tag": "file", "name": path.rsplit("/", 1)[-1],
                "id": f"id:{i}", "rev": f"{i:011x}",
                "path_lower": path.lower(), "path_display": path,
                "client_modified": "2026-01-01T00:00:00Z",
                "server_modified": "2026-01-01T00:00:00Z",
                "size": len(data), "content_hash": reference_hash(data),
                "is_downloadable": True,
            })


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Dropbox-Request-Id", "local-test")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            self.handle_post()
        except (BrokenPipeError, ConnectionResetError):
            if self.server.mode != "interrupt":
                raise
        except Exception as error:
            self.server.errors.append(str(error))
            self.send_json({"error": str(error)}, 500)

    def handle_post(self):
        server = self.server
        with server.lock:
            server.calls[self.path] += 1
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/oauth2/token":
            assert parse_qs(body.decode())["grant_type"] == ["refresh_token"]
            self.send_json({"access_token": "test-access", "expires_in": 14400,
                            "token_type": "bearer"})
            return
        assert self.headers["Authorization"] == "Bearer test-access"
        if self.path == "/2/users/get_current_account":
            self.send_json({
                "account_id": "dbid:" + "a" * 35,
                "name": {"given_name": "Test", "surname": "User", "familiar_name": "Test",
                         "display_name": "Test User", "abbreviated_name": "TU"},
                "email": "test@example.com", "email_verified": True, "disabled": False,
                "locale": "en", "referral_link": "https://example.com",
                "is_paired": False, "account_type": {".tag": "basic"},
                "root_info": {".tag": "user", "root_namespace_id": "1", "home_namespace_id": "1"},
            })
        elif self.path == "/2/users/get_space_usage":
            self.send_json({"used": 100, "allocation": {".tag": "individual", "allocated": 10000000}})
        elif self.path in ("/2/files/list_folder", "/2/files/list_folder/continue"):
            continuing = self.path.endswith("/continue")
            if server.mode == "scan_error" and continuing:
                self.send_json({"error_summary": "test failure"}, 500)
                return
            if continuing:
                assert json.loads(body)["cursor"] == "page2"
            else:
                assert json.loads(body)["recursive"] is True
            entries = server.metadata[2:] if continuing else server.metadata[:2]
            if server.mode == "missing_hash":
                entries = [{k: v for k, v in entry.items() if k != "content_hash"} for entry in entries]
            self.send_json({"entries": entries, "cursor": "end" if continuing else "page2",
                            "has_more": not continuing})
        elif self.path == "/2/files/download":
            revision = json.loads(self.headers["Dropbox-API-Arg"])["path"]
            assert revision.startswith("rev:")
            entry = next(e for e in server.metadata if "rev:" + e["rev"] == revision)
            path = entry["path_display"]
            with server.lock:
                server.downloads[path] += 1
                attempt = server.downloads[path]
            if server.mode == "rate_limit" and attempt == 1:
                self.send_json({"error_summary": "too_many_requests/", "error": {".tag": "too_many_requests"}},
                               429, {"Retry-After": "0"})
                return
            data = server.data[path]
            if path == "/docs/hello.txt" and (
                server.mode == "corrupt" or (server.mode == "recover" and attempt == 1)
            ):
                data = bytes([data[0] ^ 1]) + data[1:]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Dropbox-API-Result", json.dumps(entry))
            self.end_headers()
            if server.mode == "interrupt" and path == "/large.bin":
                self.wfile.write(data[:1024 * 1024])
                self.wfile.flush()
                server.transfer_started.set()
                server.release_transfer.wait(10)
                self.wfile.write(data[1024 * 1024:])
                return
            self.wfile.write(data)
        else:
            raise AssertionError(f"Unexpected endpoint: {self.path}")


@pytest.fixture
def server():
    instance = Server()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)
    assert not instance.errors


def cli_env(server, tmp_path, limit="0"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DROPBOX_")}
    env.update({
        "TEST_SERVER": f"http://127.0.0.1:{server.server_port}",
        "DROPBOX_APP_KEY": "test-key", "DROPBOX_APP_SECRET": "test-secret",
        "DROPBOX_REFRESH_TOKEN": "test-refresh",
        "DROPBOX_BACKUP_DEST": str(tmp_path / "destination"),
        "DROPBOX_MAX_RETRIES": "2", "DROPBOX_MAX_GB_PER_RUN": limit,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    })
    return env


def run_cli(server, tmp_path, dry=False, limit="0"):
    return subprocess.run(
        [sys.executable, "-c", RUNNER], cwd=tmp_path, env=cli_env(server, tmp_path, limit),
        input=f"y\nn\n{'y' if dry else 'n'}\ny\ny\n", text=True,
        capture_output=True, timeout=30,
    )


def assert_files(server, tmp_path):
    root = tmp_path / "destination"
    excluded = ("node_modules", "DerivedData", "Pods")
    expected = {path: data for path, data in server.data.items()
                if not any(f"/{name}/" in path for name in excluded)}
    for path, data in expected.items():
        assert (root / path.lstrip("/")).read_bytes() == data
    for name in excluded:
        assert not (root / "project" / name).exists()
    assert not list(root.rglob(".dbxpull-*.part"))


def test_cli_download_resume_and_repair(server, tmp_path):
    first = run_cli(server, tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "All selected files match Dropbox content hashes" in first.stdout
    assert_files(server, tmp_path)
    assert len(server.downloads) == 6
    before = server.downloads.copy()
    second = run_cli(server, tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert server.downloads == before  # Resume does no network downloads.
    corrupt = tmp_path / "destination/docs/hello.txt"
    corrupt.write_bytes(b"x" * corrupt.stat().st_size)
    third = run_cli(server, tmp_path)
    assert third.returncode == 0, third.stdout + third.stderr
    assert server.downloads["/docs/hello.txt"] == 2
    assert sum(server.downloads.values()) == 7
    assert_files(server, tmp_path)
    assert server.calls["/oauth2/token"] == 3
    assert server.calls["/2/files/list_folder/continue"] == 3


@pytest.mark.parametrize("mode", ["recover", "rate_limit"])
def test_cli_recovers_from_bad_transfer_or_rate_limit(server, tmp_path, mode):
    server.mode = mode
    result = run_cli(server, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert server.downloads["/docs/hello.txt"] == 2
    assert_files(server, tmp_path)


def test_cli_corruption_is_failure_and_preserves_previous_copy(server, tmp_path):
    server.mode = "corrupt"
    dest = tmp_path / "destination/docs/hello.txt"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"previous copy")
    result = run_cli(server, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "BACKUP INCOMPLETE" in result.stdout
    assert "All selected files match" not in result.stdout
    assert dest.read_bytes() == b"previous copy"
    assert not list(dest.parent.glob(".dbxpull-*.part"))


@pytest.mark.parametrize("mode", ["scan_error", "missing_hash"])
def test_cli_cannot_report_success_for_incomplete_or_unverifiable_scan(server, tmp_path, mode):
    server.mode = mode
    result = run_cli(server, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not server.downloads
    assert server.calls["/2/files/list_folder/continue"] >= 1
    assert "All selected files match" not in result.stdout
    if mode == "scan_error":
        assert "Scan complete!" not in result.stdout


def test_cli_dry_run_does_not_claim_verified_downloads(server, tmp_path):
    result = run_cli(server, tmp_path, dry=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY RUN COMPLETE" in result.stdout
    assert "No files were downloaded" in result.stdout
    assert "All selected files match" not in result.stdout
    assert not server.downloads
    assert not list((tmp_path / "destination").iterdir())


def test_cli_limit_returns_incomplete_status(server, tmp_path):
    result = run_cli(server, tmp_path, limit="0.000000001")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "BACKUP INCOMPLETE" in result.stdout
    assert "Run limit reached" in result.stdout
    assert "All selected files match" not in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery test")
def test_cli_interrupt_then_resume(server, tmp_path):
    server.mode = "interrupt"
    process = subprocess.Popen(
        [sys.executable, "-c", RUNNER], cwd=tmp_path, env=cli_env(server, tmp_path),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        process.stdin.write("y\nn\nn\ny\ny\n")
        process.stdin.close()
        process.stdin = None
        assert server.transfer_started.wait(10), "CLI never started the large transfer"
        process.send_signal(signal.SIGINT)
        server.release_transfer.set()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, stdout + stderr
        root = tmp_path / "destination"
        assert "BACKUP INTERRUPTED" in stdout
        assert not (root / "large.bin").exists()
        assert not list(root.rglob(".dbxpull-*.part"))
    finally:
        server.release_transfer.set()
        if process.poll() is None:
            process.kill()
            process.communicate()
    server.mode = "normal"
    result = run_cli(server, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_files(server, tmp_path)
