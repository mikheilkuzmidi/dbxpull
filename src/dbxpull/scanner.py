"""Dropbox folder scanning functionality."""

import time
from collections.abc import Callable
from functools import partial
from threading import Event
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import dropbox
    from dropbox.files import FileMetadata

from .config import Config
from .display import Colors, print_info, print_success
from .filters import should_skip_file
from .models import FilterOptions
from .utils import exponential_backoff_with_jitter, human_size, human_time, normalize_dropbox_path


class ScanError(RuntimeError):
    """Dropbox could not be fully listed; a partial scan must not be used."""


def _list_with_retries(request: Callable[[], Any], config: Config, stop_event: Event) -> Any:
    from dropbox.exceptions import ApiError, AuthError, RateLimitError

    for attempt in range(config.max_retries):
        if stop_event.is_set():
            raise InterruptedError("Scan interrupted")
        try:
            return request()
        except (ApiError, AuthError) as error:
            raise ScanError(f"Failed to scan Dropbox: {error}") from error
        except Exception as error:
            if attempt == config.max_retries - 1:
                raise ScanError(f"Failed to scan Dropbox: {error}") from error
            delay = exponential_backoff_with_jitter(
                attempt, config.backoff_base, config.backoff_factor, config.backoff_max,
            )
            if isinstance(error, RateLimitError) and error.backoff:
                delay = error.backoff
            if stop_event.wait(delay):
                raise InterruptedError("Scan interrupted") from error
    raise ScanError("No scan attempts configured")


def scan_dropbox(
    dbx: "dropbox.Dropbox",
    root_path: str,
    filters: FilterOptions,
    config: Config,
    stop_event: Event | None = None,
) -> tuple[list["FileMetadata"], int, int]:
    """
    Scan Dropbox folder recursively and return files to download.

    Args:
        dbx: Authenticated Dropbox client
        root_path: Root path to scan (empty string for entire Dropbox)
        filters: Filter options
        config: Application config

    Returns:
        Tuple of (files_to_download, skipped_dependency_count, skipped_other_count)
    """
    from dropbox.files import FileMetadata

    stop_event = stop_event if stop_event is not None else Event()

    print()
    print_info("Scanning Dropbox... this may take a while for large accounts.")
    print()

    # Normalize the path
    api_root = normalize_dropbox_path(root_path)

    files: list[FileMetadata] = []
    total_scanned = 0
    skip_dependency = 0
    skip_other = 0

    # Start listing
    result = _list_with_retries(
        lambda: dbx.files_list_folder(api_root, recursive=True), config, stop_event,
    )

    start_time = time.time()
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    while True:
        for entry in result.entries:
            if stop_event.is_set():
                raise InterruptedError("Scan interrupted")
            if not isinstance(entry, FileMetadata):
                continue

            total_scanned += 1

            # Check filters
            should_skip, reason = should_skip_file(entry, filters, config.skip_dir_names)

            if should_skip:
                if reason == "dependency":
                    skip_dependency += 1
                else:
                    skip_other += 1
                continue

            files.append(entry)

            # Update progress every 100 files
            if total_scanned % 100 == 0:
                elapsed = time.time() - start_time
                rate = total_scanned / elapsed if elapsed > 0 else 0
                spin_char = spinner[int(time.time() * 10) % len(spinner)]
                total_size = human_size(sum(f.size for f in files))

                print(
                    f"\r  {Colors.CYAN}{spin_char}{Colors.RESET} "
                    f"{total_scanned:,} scanned | "
                    f"{Colors.GREEN}{len(files):,}{Colors.RESET} to download ({total_size}) | "
                    f"{rate:.0f}/s    ",
                    end="",
                    flush=True
                )

        # Check for more results
        if not result.has_more:
            break

        cursor = result.cursor
        result = _list_with_retries(
            partial(dbx.files_list_folder_continue, cursor), config, stop_event,
        )

    # Clear the progress line
    print("\r" + " " * 80 + "\r", end="")

    # Print summary
    total_size = sum(f.size for f in files)
    print_success("Scan complete!")
    print()
    print(f"    Total scanned:      {total_scanned:,}")
    print(f"    {Colors.GREEN}To download:{Colors.RESET}         {len(files):,} ({human_size(total_size)})")
    print(f"    {Colors.MAGENTA}Skipped (deps):{Colors.RESET}     {skip_dependency:,}")
    print(f"    {Colors.YELLOW}Skipped (other):{Colors.RESET}    {skip_other:,}")

    if files:
        # Rough time estimate (assuming ~2 MB/s per thread average)
        est_speed = config.max_concurrent_downloads * 2 * 1024 * 1024
        est_seconds = total_size / est_speed
        print()
        print_info(f"Estimated time: ~{human_time(est_seconds)}")

    print()
    return files, skip_dependency, skip_other
