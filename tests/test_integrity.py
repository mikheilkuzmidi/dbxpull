"""Content-hash, stored-file verification and download failure regressions."""

from datetime import datetime
from threading import Event
from unittest.mock import Mock

import pytest
from dropbox.content_hash import DropboxContentHasher
from dropbox.exceptions import RateLimitError
from dropbox.files import FileMetadata

from dbxpull.config import Config
from dbxpull.downloader import Downloader, destination_path, run_backup
from dbxpull.integrity import BLOCK_SIZE, IntegrityError, content_hash, verify_file
from dbxpull.models import DownloadStats, FilterOptions
from dbxpull.rate_limiter import AdaptiveRateLimiter


def sdk_hash(data):
    """Use Dropbox's independent reference implementation for expected values."""
    hasher = DropboxContentHasher()
    for start in range(0, len(data), 7919):
        hasher.update(data[start:start + 7919])
    return hasher.hexdigest()


def metadata(data=b"correct bytes", path="/folder/file.txt", rev="123456789ab"):
    return FileMetadata(
        name=path.rsplit("/", 1)[-1], id="id:" + rev, rev=rev,
        client_modified=datetime(2026, 1, 1), server_modified=datetime(2026, 1, 1),
        size=len(data), path_lower=path.lower(), path_display=path,
        content_hash=sdk_hash(data),
    )


class Response:
    def __init__(self, data, stop=None, error=None):
        self.data, self.stop, self.error = data, stop, error
        self.closed = False

    def iter_content(self, chunk_size):
        yield b""  # requests can emit keep-alive chunks
        for start in range(0, len(self.data), chunk_size):
            if self.stop:
                self.stop.set()
            yield self.data[start:start + chunk_size]
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def context(tmp_path):
    config = Config(dest_root=str(tmp_path), max_retries=2, backoff_base=0,
                    backoff_max=0, min_download_delay=0, chunk_size=7)
    stats, stop, dbx = DownloadStats(), Event(), Mock()
    downloader = Downloader(dbx, AdaptiveRateLimiter(0), stats, stop, config)
    return downloader, dbx, stats, stop, config


@pytest.mark.parametrize("size", [0, 1, BLOCK_SIZE - 1, BLOCK_SIZE, BLOCK_SIZE + 1,
                                  2 * BLOCK_SIZE, 2 * BLOCK_SIZE + 37])
def test_hash_matches_dropbox_reference(tmp_path, size):
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    path = tmp_path / "fixture"
    path.write_bytes(data)
    assert content_hash(path) == (size, sdk_hash(data))
    verify_file(path, size, sdk_hash(data))


def test_empty_file_known_hash(tmp_path):
    path = tmp_path / "empty"
    path.touch()
    assert content_hash(path) == (
        0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )


@pytest.mark.parametrize("bad_hash", [None, "", "x" * 64, "a" * 63])
def test_missing_or_invalid_hash_fails_closed(tmp_path, bad_hash):
    path = tmp_path / "data"
    path.write_bytes(b"data")
    with pytest.raises(IntegrityError):
        verify_file(path, 4, bad_hash)


def test_cancellation_during_hash(tmp_path):
    stop = Event()
    stop.set()
    path = tmp_path / "file"
    path.write_bytes(b"content")
    with pytest.raises(InterruptedError):
        content_hash(path, stop)


@pytest.mark.parametrize("data", [b"", b"correct bytes", b"a" * (BLOCK_SIZE + 11)])
def test_download_verified_before_publish(context, tmp_path, data):
    downloader, dbx, stats, _, config = context
    config.chunk_size = 8191
    entry, response = metadata(data), Response(data)
    dbx.files_download.return_value = (entry, response)
    dest = tmp_path / "result"
    assert downloader.download_file(entry, dest)
    assert dest.read_bytes() == data
    assert stats.files_verified == 1
    assert response.closed
    assert stats.active_count == 0
    assert not list(tmp_path.glob("*.part"))
    dbx.files_download.assert_called_once_with("rev:" + entry.rev)


@pytest.mark.parametrize("bad", [b"wrong content", b"short", b"too long" * 10])
def test_corrupt_download_retried_then_succeeds(context, tmp_path, bad):
    downloader, dbx, stats, _, _ = context
    entry = metadata()
    responses = [Response(bad), Response(b"correct bytes")]
    dbx.files_download.side_effect = [(entry, r) for r in responses]
    dest = tmp_path / "result"
    assert downloader.download_file(entry, dest)
    assert dest.read_bytes() == b"correct bytes"
    assert stats.integrity_failures == 1
    assert stats.retries_total == 1
    assert all(r.closed for r in responses)


@pytest.mark.parametrize("existing", [False, True])
def test_retry_exhaustion_never_publishes_corruption(context, tmp_path, existing):
    downloader, dbx, stats, _, _ = context
    entry, dest = metadata(), tmp_path / "result"
    if existing:
        dest.write_bytes(b"previous version")
    responses = [Response(b"wrong content"), Response(b"wrong content")]
    dbx.files_download.side_effect = [(entry, r) for r in responses]
    assert not downloader.download_file(entry, dest)
    assert dest.read_bytes() == b"previous version" if existing else not dest.exists()
    assert stats.files_verified == 0
    assert stats.integrity_failures == 2
    assert stats.active_count == 0
    assert not list(tmp_path.glob("*.part"))
    assert all(r.closed for r in responses)


def test_verifies_disk_bytes_not_only_response(context, tmp_path, monkeypatch):
    import dbxpull.downloader as module
    downloader, dbx, stats, _, _ = context
    entry = metadata()
    dbx.files_download.side_effect = lambda *_: (entry, Response(b"correct bytes"))
    real_verify = module.verify_file

    def corrupt_then_verify(path, *args):
        path.write_bytes(b"wrong content")
        return real_verify(path, *args)

    monkeypatch.setattr(module, "verify_file", corrupt_then_verify)
    assert not downloader.download_file(entry, tmp_path / "result")
    assert stats.integrity_failures == 2
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("field,value", [("rev", "abcdef12345"), ("id", "id:other"),
                                         ("size", 20), ("content_hash", "0" * 64)])
def test_response_metadata_must_match_scanned_revision(context, tmp_path, field, value):
    downloader, dbx, stats, _, _ = context
    returned = metadata()
    setattr(returned, field, value)
    response = Response(b"correct bytes")
    dbx.files_download.return_value = (returned, response)
    assert not downloader.download_file(metadata(), tmp_path / "result")
    assert response.closed
    assert stats.files_verified == 0


def test_interruption_cleans_partial_and_closes_response(context, tmp_path):
    downloader, dbx, stats, stop, _ = context
    entry, response = metadata(), Response(b"correct bytes", stop=stop)
    dbx.files_download.return_value = (entry, response)
    dest = tmp_path / "result"
    assert not downloader.download_file(entry, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part"))
    assert response.closed
    assert stats.active_count == 0


def test_interrupted_stream_retries(context, tmp_path):
    downloader, dbx, stats, _, _ = context
    entry = metadata()
    responses = [Response(b"correct", error=ConnectionError("connection lost")),
                 Response(b"correct bytes")]
    dbx.files_download.side_effect = [(entry, r) for r in responses]
    assert downloader.download_file(entry, tmp_path / "result")
    assert stats.retries_total == 1
    assert all(r.closed for r in responses)


def test_rate_limit_retries_are_bounded(context, tmp_path):
    downloader, dbx, stats, _, _ = context
    dbx.files_download.side_effect = RateLimitError("request", backoff=0)
    assert not downloader.download_file(metadata(), tmp_path / "result")
    assert dbx.files_download.call_count == 2
    assert stats.rate_limit_hits == 2
    assert stats.retries_total == 1


def test_write_failure_keeps_existing_destination(context, tmp_path, monkeypatch):
    downloader, dbx, _, _, _ = context
    entry = metadata()
    dbx.files_download.side_effect = lambda *_: (entry, Response(b"correct bytes"))
    dest = tmp_path / "result"
    dest.write_bytes(b"old file")
    monkeypatch.setattr("dbxpull.downloader.os.fsync", Mock(side_effect=OSError("disk full")))
    assert not downloader.download_file(entry, dest)
    assert dest.read_bytes() == b"old file"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("existing,expected_downloads", [(b"correct bytes", 0),
                                                        (b"wrong content", 1), (b"short", 1)])
def test_resume_checks_hash_not_only_size(context, tmp_path, existing, expected_downloads):
    _, dbx, stats, stop, config = context
    entry = metadata(path="/file.txt")
    (tmp_path / "file.txt").write_bytes(existing)
    dbx.files_download.return_value = (entry, Response(b"correct bytes"))
    run_backup(dbx, [entry], FilterOptions(), stats, stop, config)
    assert stats.files_downloaded == expected_downloads
    assert stats.files_skipped_exists == 1 - expected_downloads
    assert stats.files_verified == 1
    assert stats.files_failed == 0
    assert (tmp_path / "file.txt").read_bytes() == b"correct bytes"


def test_real_part_filename_does_not_collide(context, tmp_path):
    _, dbx, stats, stop, config = context
    entries = [metadata(b"one", "/file", "11111111111"),
               metadata(b"two", "/file.part", "22222222222")]
    mapping = {"rev:" + e.rev: (e, Response(data)) for e, data in zip(entries, [b"one", b"two"], strict=True)}
    dbx.files_download.side_effect = lambda path: mapping[path]
    run_backup(dbx, entries, FilterOptions(), stats, stop, config)
    assert (tmp_path / "file").read_bytes() == b"one"
    assert (tmp_path / "file.part").read_bytes() == b"two"
    assert stats.files_verified == 2


def test_dry_run_does_not_claim_download_or_verification(context, tmp_path):
    _, dbx, stats, stop, config = context
    run_backup(dbx, [metadata()], FilterOptions(dry_run=True), stats, stop, config)
    assert stats.files_downloaded == stats.files_verified == stats.bytes_downloaded == 0
    assert stats.files_planned == 1
    dbx.files_download.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_parallel_budget_never_reports_full_completion(context):
    _, dbx, stats, stop, config = context
    config.max_gb_per_run = 13 / 1e9
    entries = [metadata(path=f"/file{i}", rev=f"{i + 1:011x}") for i in range(10)]
    mapping = {"rev:" + e.rev: e for e in entries}
    dbx.files_download.side_effect = lambda path: (mapping[path], Response(b"correct bytes"))
    run_backup(dbx, entries, FilterOptions(), stats, stop, config)
    assert stats.files_downloaded == stats.files_verified == 1
    assert stats.bytes_downloaded == 13
    assert stats.files_deferred == 9


@pytest.mark.parametrize("path", ["/../outside", "//absolute", "/folder/../../escape", "/C:/file", "/a\\b"])
def test_rejects_unsafe_paths(tmp_path, path):
    with pytest.raises(ValueError):
        destination_path(tmp_path, path)


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        destination_path(root, "/link/outside")


def test_missing_hash_existing_file_is_failure(context, tmp_path):
    _, dbx, stats, stop, config = context
    entry = metadata(path="/file")
    entry.content_hash = None
    (tmp_path / "file").write_bytes(b"correct bytes")
    run_backup(dbx, [entry], FilterOptions(), stats, stop, config)
    assert stats.files_failed == 1
    assert stats.files_verified == stats.files_skipped_exists == 0
    dbx.files_download.assert_not_called()
