"""Configuration management for Dropbox Backup."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Dependency/build folders to skip by default
DEFAULT_SKIP_DIRS: set[str] = {
    # JavaScript/Node
    "node_modules", ".npm", ".yarn", ".pnpm-store", ".bower_components",
    # Python
    "venv", ".venv", "env", ".env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", ".nox", "site-packages", ".eggs",
    # Build outputs
    "build", "dist", "out", "target", "_build", ".build",
    # Frontend frameworks
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
    ".cache", ".webpack", ".angular", ".expo",
    # IDEs
    ".idea", ".vscode", ".vs", ".eclipse", ".settings",
    # Version control
    ".git", ".hg", ".svn",
    # Other package managers
    "vendor", "bower_components", ".gradle", ".maven", "Pods",
    "DerivedData", "cmake-build-debug", "cmake-build-release",
    # Temp/logs
    "logs", ".logs", "tmp", ".tmp", "temp", ".temp",
}


def _load_env_file(path: Path | None = None) -> None:
    """Load environment variables from a .env file if present.

    This is a minimal loader that supports simple ``KEY=VALUE`` lines.
    Existing environment variables are not overridden.
    """

    try:
        env_path = path or (Path.cwd() / ".env")
        if not env_path.exists():
            return

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            # Strip optional quotes
            if (value.startswith("\"") and value.endswith("\"")) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]

            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # Failing to load .env should never crash the app; ignore errors.
        return


@dataclass
class Config:
    """Application configuration."""

    # Dropbox settings
    access_token: str = ""
    root_path: str = ""

    # Local settings
    dest_root: str = ""

    # Limits
    max_gb_per_run: float = 0.0  # 0 = unlimited

    # Performance tuning
    max_concurrent_downloads: int = 6
    min_download_delay: float = 0.05
    download_timeout: int = 300
    chunk_size: int = 1024 * 1024  # 1MB

    # Retry settings
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0
    max_retries: int = 5

    # Rate limiting
    rate_limit_window: float = 60.0
    rate_limit_threshold: int = 3

    # Directories to skip
    skip_dir_names: set[str] = field(default_factory=lambda: DEFAULT_SKIP_DIRS.copy())

    @classmethod
    def from_env(cls) -> "Config":
        """Create config from environment variables."""
        # Lazily load variables from a local .env file, if present.
        # Explicitly-set environment variables take precedence.
        _load_env_file()

        return cls(
            access_token=os.getenv("DROPBOX_ACCESS_TOKEN", ""),
            root_path=os.getenv("DROPBOX_ROOT_PATH", ""),
            dest_root=os.getenv("DROPBOX_BACKUP_DEST", ""),
            max_gb_per_run=float(os.getenv("DROPBOX_MAX_GB_PER_RUN", "0")),
            max_concurrent_downloads=int(os.getenv("DROPBOX_CONCURRENT_DOWNLOADS", "6")),
            download_timeout=int(os.getenv("DROPBOX_TIMEOUT", "300")),
            max_retries=int(os.getenv("DROPBOX_MAX_RETRIES", "5")),
        )

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of errors."""
        errors = []

        if not self.access_token:
            errors.append("DROPBOX_ACCESS_TOKEN is required")
        elif "PASTE" in self.access_token:
            errors.append("DROPBOX_ACCESS_TOKEN contains placeholder text")

        if not self.dest_root:
            errors.append("Destination directory is required")
        else:
            dest = Path(self.dest_root)
            if dest.exists() and not dest.is_dir():
                errors.append(f"Destination exists but is not a directory: {dest}")

        if self.max_concurrent_downloads < 1:
            errors.append("max_concurrent_downloads must be at least 1")
        elif self.max_concurrent_downloads > 20:
            errors.append("max_concurrent_downloads should not exceed 20")

        if self.download_timeout < 30:
            errors.append("download_timeout should be at least 30 seconds")

        return errors

    def ensure_dest_exists(self) -> Path:
        """Ensure destination directory exists. Returns Path."""
        dest = Path(self.dest_root)
        dest.mkdir(parents=True, exist_ok=True)
        return dest
