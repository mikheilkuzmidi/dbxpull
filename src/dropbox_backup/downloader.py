"""Download engine for Dropbox Backup."""

import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import dropbox
    from dropbox.files import FileMetadata

from .config import Config
from .display import Colors, ProgressDisplay, print_header, print_info, print_warning
from .models import DownloadStats, FilterOptions
from .rate_limiter import AdaptiveRateLimiter
from .utils import exponential_backoff_with_jitter, human_size

logger = logging.getLogger(__name__)


class Downloader:
    """Handles individual file downloads with retry logic."""

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
        """
        Download a single file with retry logic.

        Args:
            entry: Dropbox file metadata
            dest: Local destination path

        Returns:
            True if successful, False otherwise
        """
        from dropbox.exceptions import ApiError, RateLimitError

        if self.stop_event.is_set():
            return False

        # Ensure parent directory exists
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Use a temp file during download
        tmp_path = dest.with_suffix(dest.suffix + ".part")
        slot = self.stats.start_download(entry.path_display, entry.size)

        try:
            for attempt in range(self.config.max_retries):
                if self.stop_event.is_set():
                    return False

                try:
                    # Wait for rate limiter
                    self.limiter.wait()

                    logger.debug(
                        "Downloading: %s (%s)",
                        entry.path_display,
                        human_size(entry.size)
                    )

                    # Download the file
                    _, response = self.dbx.files_download(entry.path_lower)

                    # Stream to temp file
                    downloaded = 0
                    with open(tmp_path, "wb") as f:
                        for chunk in response.iter_content(self.config.chunk_size):
                            if self.stop_event.is_set():
                                return False
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                self.stats.update_download(slot, downloaded)

                    # Move temp file to final destination
                    tmp_path.replace(dest)
                    self.limiter.record_success()
                    return True

                except RateLimitError as e:
                    self.stats.increment("rate_limit_hits")
                    self.stats.increment("retries_total")
                    self.limiter.record_rate_limit()

                    wait_time = (
                        e.backoff
                        if hasattr(e, "backoff") and e.backoff
                        else exponential_backoff_with_jitter(
                            attempt,
                            self.config.backoff_base,
                            self.config.backoff_factor,
                            self.config.backoff_max,
                        )
                    )

                    logger.warning(
                        "Rate limited on %s, waiting %.1fs",
                        entry.path_display,
                        wait_time
                    )
                    time.sleep(wait_time)

                except (ApiError, Exception) as e:
                    self.stats.increment("retries_total")

                    if "too_many" in str(e).lower():
                        self.stats.increment("rate_limit_hits")
                        self.limiter.record_rate_limit()

                    if attempt < self.config.max_retries - 1:
                        wait_time = exponential_backoff_with_jitter(
                            attempt,
                            self.config.backoff_base,
                            self.config.backoff_factor,
                            self.config.backoff_max,
                        )
                        logger.warning(
                            "Error downloading %s: %s, retrying in %.1fs",
                            entry.path_display,
                            e,
                            wait_time
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(
                            "Failed to download %s after %d attempts: %s",
                            entry.path_display,
                            self.config.max_retries,
                            e
                        )
                        return False

                finally:
                    # Clean up partial file on failure
                    if tmp_path.exists() and not dest.exists():
                        with contextlib.suppress(OSError):
                            tmp_path.unlink()

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
    """
    Run the backup process with parallel downloads.

    Args:
        dbx: Authenticated Dropbox client
        files: List of files to download
        filters: Filter options
        stats: Download statistics tracker
        stop_event: Event to signal stop
        config: Application config
    """
    max_bytes = int(config.max_gb_per_run * 1e9) if config.max_gb_per_run > 0 else 0

    # Initialize stats
    stats.files_total = len(files)
    stats.bytes_total = sum(f.size for f in files)

    # Create rate limiter and progress display
    limiter = AdaptiveRateLimiter(config.min_download_delay)
    display = ProgressDisplay(stats, limiter, config.max_concurrent_downloads)
    downloader = Downloader(dbx, limiter, stats, stop_event, config)

    print_header("Downloading")
    print()

    if filters.dry_run:
        print_warning("DRY RUN MODE - No files will be downloaded")

    print_info(f"Started: {datetime.now().strftime('%H:%M:%S')}")
    print_info(f"Threads: {config.max_concurrent_downloads}")
    print()

    # Hide cursor and start display
    print(Colors.HIDE_CURSOR, end="", flush=True)
    display.start()

    bytes_this_run = 0
    limit_reached = False
    dest_root = Path(config.dest_root)

    def process_file(entry: "FileMetadata") -> tuple:
        """Process a single file."""
        nonlocal bytes_this_run, limit_reached

        if stop_event.is_set() or limit_reached:
            return entry, "skip"

        # Determine local destination
        dest = dest_root / entry.path_display.lstrip("/")

        # Check if file already exists with correct size
        if dest.exists():
            try:
                if dest.stat().st_size == entry.size:
                    return entry, "exists"
            except OSError:
                pass

        # Check byte limit
        if max_bytes > 0 and bytes_this_run >= max_bytes:
            limit_reached = True
            return entry, "limit"

        # Dry run mode
        if filters.dry_run:
            return entry, "dry"

        # Actually download the file
        success = downloader.download_file(entry, dest)

        if success:
            bytes_this_run += entry.size

        return entry, "ok" if success else "fail"

    try:
        with ThreadPoolExecutor(max_workers=config.max_concurrent_downloads) as executor:
            futures = {executor.submit(process_file, f): f for f in files}

            for future in as_completed(futures):
                if stop_event.is_set():
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    break

                try:
                    entry, result = future.result()

                    if result == "exists":
                        stats.increment("files_skipped_exists")
                        stats.increment("bytes_skipped", entry.size)
                    elif result in ("ok", "dry"):
                        stats.increment("files_downloaded")
                        stats.increment("bytes_downloaded", entry.size)
                    elif result == "fail":
                        stats.increment("files_failed")
                    # "skip" and "limit" don't update stats

                except Exception as e:
                    logger.error("Unexpected error processing file: %s", e)
                    stats.increment("files_failed")

    finally:
        display.stop()
        print(Colors.SHOW_CURSOR, end="", flush=True)
