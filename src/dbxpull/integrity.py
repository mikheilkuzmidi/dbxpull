"""Verify local bytes using Dropbox's SHA-256 content hash."""

import hashlib
import re
from pathlib import Path
from threading import Event

BLOCK_SIZE = 4 * 1024 * 1024


class IntegrityError(ValueError):
    """A file cannot be verified against Dropbox metadata."""


def require_content_hash(value: str | None) -> str:
    """Fail closed if Dropbox did not supply a usable content hash."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise IntegrityError("Missing or invalid Dropbox content_hash; file cannot be verified")
    return value


def content_hash(path: Path, stop_event: Event | None = None) -> tuple[int, str]:
    """Read a file in bounded memory and return (bytes read, Dropbox hash).

    Dropbox hashes each 4 MiB block, then hashes the concatenated binary
    digests. An empty file has no blocks. This is not ordinary file SHA-256.
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Verification interrupted")
            block = source.read(BLOCK_SIZE)
            if not block:
                break
            size += len(block)
            digest.update(hashlib.sha256(block).digest())
    return size, digest.hexdigest()


def verify_file(
    path: Path, expected_size: int, expected_hash: str | None,
    stop_event: Event | None = None,
) -> None:
    """Raise IntegrityError unless the stored file matches both size and hash."""
    expected_hash = require_content_hash(expected_hash)
    if not path.is_file() or path.stat().st_size != expected_size:
        raise IntegrityError("File size does not match Dropbox metadata")
    size, actual_hash = content_hash(path, stop_event)
    if size != expected_size or actual_hash != expected_hash:
        raise IntegrityError("File content hash does not match Dropbox metadata")
