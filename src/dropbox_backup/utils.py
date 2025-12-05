"""Utility functions for Dropbox Backup."""

import random
import shutil
import sys


def human_size(num_bytes: int, precision: int = 2) -> str:
    """Convert bytes to human-readable string (e.g., '1.5 GB')."""
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(num) < 1024.0:
            return f"{num:.{precision}f} {unit}"
        num /= 1024.0
    return f"{num:.{precision}f} EB"


def human_time(seconds: float | None) -> str:
    """Convert seconds to human-readable duration (e.g., '2h 15m')."""
    if seconds is None:
        return "calculating..."
    if seconds < 0:
        return "unknown"

    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h {m}m"


def human_speed(bytes_per_sec: float) -> str:
    """Convert bytes/sec to human-readable speed (e.g., '2.5 MB/s')."""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec / (1024 * 1024):.2f} MB/s"


def truncate_path(path: str, max_len: int = 40) -> str:
    """Truncate a path for display, keeping the end."""
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def get_terminal_width() -> int:
    """Get terminal width, defaulting to 80 if unavailable."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def is_tty() -> bool:
    """Check if stdout is a terminal (supports colors/cursor control)."""
    return sys.stdout.isatty()


def exponential_backoff_with_jitter(
    attempt: int,
    base: float = 1.0,
    factor: float = 2.0,
    max_delay: float = 60.0,
) -> float:
    """Calculate delay with exponential backoff and jitter."""
    delay = min(max_delay, base * (factor ** attempt))
    return random.uniform(0, delay)


def parse_size(size_str: str) -> int:
    """
    Parse a human-readable size string to bytes.

    Examples:
        "100" -> 100
        "1KB" -> 1024
        "1.5MB" -> 1572864
        "2GB" -> 2147483648
    """
    size_str = size_str.strip().upper()

    multipliers = {
        "B": 1,
        "K": 1024, "KB": 1024,
        "M": 1024**2, "MB": 1024**2,
        "G": 1024**3, "GB": 1024**3,
        "T": 1024**4, "TB": 1024**4,
    }

    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if size_str.endswith(suffix):
            num = float(size_str[:-len(suffix)].strip())
            return int(num * mult)

    return int(float(size_str))


def normalize_dropbox_path(path: str) -> str:
    """Normalize a Dropbox path (ensure leading slash, no trailing)."""
    if not path or path == "/":
        return ""

    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path

    return path.rstrip("/")
