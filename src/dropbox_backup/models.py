"""Data models for Dropbox Backup."""

import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ActiveDownload:
    """Track an active download in progress."""

    slot: int
    path: str
    total_bytes: int
    downloaded_bytes: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def progress_percent(self) -> float:
        """Get download progress as percentage."""
        if self.total_bytes <= 0:
            return 0.0
        return (self.downloaded_bytes / self.total_bytes) * 100

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed time since download started."""
        return time.time() - self.start_time

    @property
    def speed_bps(self) -> float:
        """Get current download speed in bytes per second."""
        elapsed = self.elapsed_seconds
        if elapsed < 0.1:
            return 0.0
        return self.downloaded_bytes / elapsed


@dataclass
class DownloadStats:
    """Track download statistics with thread safety."""

    files_total: int = 0
    files_downloaded: int = 0
    files_skipped_exists: int = 0
    files_skipped_filter: int = 0
    files_skipped_dependency: int = 0
    files_failed: int = 0
    bytes_total: int = 0
    bytes_downloaded: int = 0
    bytes_skipped: int = 0
    start_time: float = field(default_factory=time.time)
    rate_limit_hits: int = 0
    retries_total: int = 0

    _lock: Lock = field(default_factory=Lock)
    _active: dict[int, ActiveDownload] = field(default_factory=dict)
    _next_slot: int = 0

    def increment(self, attr: str, value: int = 1) -> None:
        """Thread-safe increment of a stat attribute."""
        with self._lock:
            current = getattr(self, attr)
            setattr(self, attr, current + value)

    def start_download(self, path: str, total_bytes: int) -> int:
        """Register a new active download. Returns slot ID."""
        with self._lock:
            slot = self._next_slot
            self._next_slot += 1
            self._active[slot] = ActiveDownload(slot, path, total_bytes)
            return slot

    def update_download(self, slot: int, downloaded: int) -> None:
        """Update progress for an active download."""
        with self._lock:
            if slot in self._active:
                self._active[slot].downloaded_bytes = downloaded

    def finish_download(self, slot: int) -> None:
        """Remove a completed download from active tracking."""
        with self._lock:
            self._active.pop(slot, None)

    def get_active_downloads(self) -> list[ActiveDownload]:
        """Get list of active downloads sorted by slot."""
        with self._lock:
            return sorted(self._active.values(), key=lambda x: x.slot)

    @property
    def active_count(self) -> int:
        """Get count of currently active downloads."""
        with self._lock:
            return len(self._active)

    @property
    def elapsed_seconds(self) -> float:
        """Get total elapsed time since backup started."""
        return time.time() - self.start_time

    @property
    def speed_bps(self) -> float:
        """Get overall download speed in bytes per second."""
        if self.elapsed_seconds < 0.1:
            return 0.0
        return self.bytes_downloaded / self.elapsed_seconds

    @property
    def eta_seconds(self) -> float | None:
        """Estimate time remaining in seconds."""
        if self.speed_bps < 100:
            return None
        remaining = self.bytes_total - self.bytes_downloaded - self.bytes_skipped
        if remaining <= 0:
            return 0.0
        return remaining / self.speed_bps

    @property
    def files_processed(self) -> int:
        """Get total files processed (downloaded + skipped)."""
        return (
            self.files_downloaded
            + self.files_skipped_exists
            + self.files_skipped_filter
            + self.files_skipped_dependency
            + self.files_failed
        )

    @property
    def is_complete(self) -> bool:
        """Check if all files have been processed."""
        return self.files_processed >= self.files_total


@dataclass
class FilterOptions:
    """File filtering options for backup."""

    skip_dependencies: bool = True
    include_extensions: set[str] = field(default_factory=set)
    exclude_extensions: set[str] = field(default_factory=set)
    min_size_bytes: int = 0
    max_size_bytes: int = 0  # 0 = no limit
    dry_run: bool = False

    def __post_init__(self) -> None:
        """Normalize extensions to lowercase without dots."""
        self.include_extensions = {
            ext.lower().lstrip(".") for ext in self.include_extensions
        }
        self.exclude_extensions = {
            ext.lower().lstrip(".") for ext in self.exclude_extensions
        }

    def to_dict(self) -> dict:
        """Convert to dictionary for logging/display."""
        return {
            "skip_dependencies": self.skip_dependencies,
            "include_extensions": list(self.include_extensions) or None,
            "exclude_extensions": list(self.exclude_extensions) or None,
            "min_size_bytes": self.min_size_bytes or None,
            "max_size_bytes": self.max_size_bytes or None,
            "dry_run": self.dry_run,
        }
