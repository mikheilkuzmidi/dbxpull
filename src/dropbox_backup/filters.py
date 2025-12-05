"""File filtering logic for Dropbox Backup."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dropbox.files import FileMetadata

from .config import DEFAULT_SKIP_DIRS
from .models import FilterOptions


def should_skip_file(
    entry: "FileMetadata",
    filters: FilterOptions,
    skip_dirs: set[str] = DEFAULT_SKIP_DIRS,
) -> tuple[bool, str]:
    """
    Determine if a file should be skipped based on filter options.

    Args:
        entry: Dropbox file metadata
        filters: Filter options
        skip_dirs: Set of directory names to skip

    Returns:
        Tuple of (should_skip: bool, reason: str)
        Reason is empty string if not skipping.
    """
    # Check if in a dependency directory
    if filters.skip_dependencies:
        path_parts = entry.path_lower.split("/")
        for part in path_parts:
            if part in skip_dirs:
                return True, "dependency"

    # Get file extension
    ext = Path(entry.name).suffix.lower().lstrip(".")

    # Check include filter (if set, only include matching extensions)
    if filters.include_extensions and ext not in filters.include_extensions:
        return True, "extension"

    # Check exclude filter
    if filters.exclude_extensions and ext in filters.exclude_extensions:
        return True, "extension"

    # Check minimum size
    if filters.min_size_bytes > 0 and entry.size < filters.min_size_bytes:
        return True, "size"

    # Check maximum size
    if filters.max_size_bytes > 0 and entry.size > filters.max_size_bytes:
        return True, "size"

    return False, ""


def parse_extensions(ext_string: str) -> set[str]:
    """
    Parse a comma-separated extension string.

    Args:
        ext_string: String like "jpg,png,gif" or ".jpg, .png, .gif"

    Returns:
        Set of normalized extensions (lowercase, no dots)
    """
    if not ext_string:
        return set()

    extensions = set()
    for ext in ext_string.split(","):
        ext = ext.strip().lower().lstrip(".")
        if ext:
            extensions.add(ext)

    return extensions


def get_file_category(filename: str) -> str:
    """
    Get the category of a file based on its extension.

    Returns one of: 'document', 'image', 'video', 'audio', 'code', 'archive', 'other'
    """
    ext = Path(filename).suffix.lower().lstrip(".")

    categories = {
        "document": {"pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx"},
        "image": {"jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "ico", "tiff", "raw"},
        "video": {"mp4", "avi", "mkv", "mov", "wmv", "flv", "webm", "m4v"},
        "audio": {"mp3", "wav", "flac", "aac", "ogg", "wma", "m4a"},
        "code": {"py", "js", "ts", "jsx", "tsx", "java", "c", "cpp", "h", "cs", "go", "rs", "rb", "php"},
        "archive": {"zip", "tar", "gz", "rar", "7z", "bz2", "xz"},
    }

    for category, extensions in categories.items():
        if ext in extensions:
            return category

    return "other"
