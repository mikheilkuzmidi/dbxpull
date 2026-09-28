"""Download engine for dbxpull."""

import contextlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import dropbox
    from dropbox.files import FileMetadata

from .config import Config
from .display import Colors, ProgressDisplay, print_header, print_info, print_warning
from .integrity import IntegrityError, require_content_hash, verify_file
from .models import DownloadStats, FilterOptions
from .rate_limiter import AdaptiveRateLimiter
from .utils import exponential_backoff_with_jitter, human_size

logger = logging.getLogger(__name__)


def destination_path(root: Path, remote_path: str) -> Path:
    """Reject paths or existing symlinks that could write outside the destination."""
    parts = remote_path.removeprefix("/").split("/")
    if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
        raise ValueError(f"Unsafe destination path: {remote_path}")
    dest = root.joinpath(*parts)
    if not dest.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Destination escapes root: {remote_path}")
    return dest


class Downloader:
    """Download, read back and verify a file before replacing its destination."""

    def __init__(
        self,
        dbx: "dropbox.Dropbox",
        limiter: AdaptiveRateLimiter,
        stats: DownloadStats,
        stop_event: Event,
        config: Config,
    ):
        self.dbx = dbx
        self.limiter = limiter
        self.stats = stats
        self.stop_event = stop_event
        self.config = config

    def download_file(self, entry: "FileMetadata", dest: Path) -> bool:
        """Return True only after stored bytes match the scanned Dropbox revision."""
        from dropbox.exceptions import RateLimitError

        if self.stop_event.is_set():
            return False

        slot = self.stats.start_download(entry.path_display, entry.size)
        try:
            expected_hash = require_content_hash(entry.content_hash)
            if not entry.rev:
                raise IntegrityError("Missing Dropbox revision; file cannot be verified")
            dest.parent.mkdir(parents=True, exist_ok=True)
            for attempt in range(self.config.max_retries):
                tmp_path = None
                try:
                    self.limiter.wait()
                    if self.stop_event.is_set():
                        return False
                    self.stats.update_download(slot, 0)
                    logger.debug("Downloading: %s (%s)", entry.path_display, human_size(entry.size))
                    metadata, response = self.dbx.files_download(f"rev:{entry.rev}")
                    with contextlib.closing(response):
                        if (
                            metadata.id != entry.id or metadata.rev != entry.rev
                            or metadata.size != entry.size
                            or require_content_hash(metadata.content_hash) != expected_hash
                        ):
                            raise IntegrityError("Download metadata does not match scanned revision")
                        # A unique sibling avoids collisions with real files named *.part.
                        # Keeping it on the destination volume also makes replace atomic.
                        with NamedTemporaryFile(
                            mode="wb", dir=dest.parent, prefix=".dbxpull-", suffix=".part",
                            delete=False,
                        ) as target:
                            tmp_path = Path(target.name)
                            downloaded = 0
                            for chunk in response.iter_content(self.config.chunk_size):
                                if self.stop_event.is_set():
                                    return False
                                if chunk:
                                    downloaded += len(chunk)
                                    if downloaded > entry.size:
                                        raise IntegrityError("Download exceeds expected size")
                                    target.write(chunk)
                                    self.stats.update_download(slot, downloaded)
                            target.flush()
                            os.fsync(target.fileno())
                    # Read back the stored file, not just the incoming network buffers.
                    verify_file(tmp_path, entry.size, expected_hash, self.stop_event)
                    if self.stop_event.is_set():
                        return False
                    tmp_path.replace(dest)
                    self.stats.increment("files_verified")
                    self.limiter.record_success()
                    return True
                except InterruptedError:
                    return False
                except Exception as error:
                    if isinstance(error, IntegrityError):
                        self.stats.increment("integrity_failures")
                    if isinstance(error, RateLimitError):
                        self.stats.increment("rate_limit_hits")
                        self.limiter.record_rate_limit()
                    if attempt == self.config.max_retries - 1:
                        logger.error("Failed to download %s after %d attempts: %s",
                                     entry.path_display, self.config.max_retries, error)
                        return False
                    self.stats.increment("retries_total")
                    wait_time = exponential_backoff_with_jitter(
                        attempt, self.config.backoff_base,
                        self.config.backoff_factor, self.config.backoff_max,
                    )
                    if isinstance(error, RateLimitError) and error.backoff:
                        wait_time = error.backoff
                    logger.warning("Error downloading %s: %s, retrying in %.1fs",
                                   entry.path_display, error, wait_time)
                    if self.stop_event.wait(wait_time):
                        return False
                finally:
                    if tmp_path is not None:
                        with contextlib.suppress(FileNotFoundError):
                            tmp_path.unlink()
            return False
        except (OSError, IntegrityError) as error:
            logger.error("Cannot download %s: %s", entry.path_display, error)
            if isinstance(error, IntegrityError):
                self.stats.increment("integrity_failures")
            return False
        finally:
            self.stats.finish_download(slot)


def run_backup(
    dbx: "dropbox.Dropbox",
    files: list["FileMetadata"],
    filters: FilterOptions,
    stats: DownloadStats,
    stop_event: Event,
    config: Config,
) -> None:
    """Run parallel downloads with mandatory verification on downloads and resume."""
    max_bytes = int(config.max_gb_per_run * 1e9) if config.max_gb_per_run > 0 else 0
    stats.files_total = len(files)
    stats.bytes_total = sum(f.size for f in files)
    limiter = AdaptiveRateLimiter(config.min_download_delay)
    display = ProgressDisplay(stats, limiter, config.max_concurrent_downloads)
    downloader = Downloader(dbx, limiter, stats, stop_event, config)

    print_header("Downloading")
    print()
    if filters.dry_run:
        print_warning("DRY RUN MODE - No files will be downloaded")
    print_info("Content-hash verification enabled for downloads and existing files")
    print_info(f"Started: {datetime.now().strftime('%H:%M:%S')}")
    print_info(f"Threads: {config.max_concurrent_downloads}")
    print()

    print(Colors.HIDE_CURSOR, end="", flush=True)
    display.start()
    reserved_bytes = 0
    budget_lock = Lock()
    dest_root = Path(config.dest_root)

    def process_file(entry: "FileMetadata") -> tuple:
        nonlocal reserved_bytes
        if stop_event.is_set():
            return entry, "skip"
        dest = destination_path(dest_root, entry.path_display)
        require_content_hash(entry.content_hash)
        if dest.exists():
            try:
                verify_file(dest, entry.size, entry.content_hash, stop_event)
                stats.increment("files_verified")
                return entry, "exists"
            except InterruptedError:
                return entry, "skip"
            except (OSError, IntegrityError) as error:
                logger.warning("Local file needs downloading: %s: %s", entry.path_display, error)

        # Reserve the whole file before starting, so concurrent workers cannot
        # exceed the configured budget. Files larger than the remainder are deferred.
        with budget_lock:
            if max_bytes > 0 and reserved_bytes + entry.size > max_bytes:
                return entry, "limit"
            reserved_bytes += entry.size
        if filters.dry_run:
            return entry, "dry"
        success = downloader.download_file(entry, dest)
        if not success:
            with budget_lock:
                reserved_bytes -= entry.size
        return entry, "ok" if success else "fail"

    try:
        with ThreadPoolExecutor(max_workers=config.max_concurrent_downloads) as executor:
            futures = {executor.submit(process_file, f): f for f in files}
            for future in as_completed(futures):
                if stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    entry, result = future.result()
                    if result == "exists":
                        stats.increment("files_skipped_exists")
                        stats.increment("bytes_skipped", entry.size)
                    elif result == "ok":
                        stats.increment("files_downloaded")
                        stats.increment("bytes_downloaded", entry.size)
                    elif result == "dry":
                        stats.increment("files_planned")
                        stats.increment("bytes_planned", entry.size)
                    elif result == "limit":
                        stats.increment("files_deferred")
                    elif result == "fail":
                        stats.increment("files_failed")
                except Exception as error:
                    logger.error("Failed processing %s: %s", futures[future].path_display, error)
                    stats.increment("files_failed")
    finally:
        display.stop()
        print(Colors.SHOW_CURSOR, end="", flush=True)
