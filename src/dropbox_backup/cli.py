"""Command-line interface for Dropbox Backup."""

import atexit
import logging
import shutil
import signal
import sys
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    import dropbox

    from .models import FilterOptions


def select_folder_dialog() -> str | None:
    """Open a folder picker dialog. Returns path or None if cancelled."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()  # Hide the main window
        root.attributes("-topmost", True)  # Bring dialog to front

        folder = filedialog.askdirectory(
            title="Select Backup Destination Folder",
            mustexist=False,
        )

        root.destroy()
        return folder if folder else None
    except ImportError:
        return None
    except Exception:
        return None


def prompt_for_destination() -> str | None:
    """Prompt user for destination - try GUI first, fall back to terminal."""
    from .display import Colors, print_info, print_warning

    print_info("No destination folder configured.")
    print()

    # Try GUI dialog first
    print_info("Opening folder picker...")
    folder = select_folder_dialog()

    if folder:
        return folder

    # Fall back to terminal input
    print_warning("Could not open folder picker. Please enter path manually.")
    try:
        path = input(f"  {Colors.CYAN}?{Colors.RESET} Enter backup destination path: ").strip()
        return path if path else None
    except (EOFError, KeyboardInterrupt):
        return None


logger = logging.getLogger(__name__)


def setup_logging(log_file: Path) -> None:
    """Configure logging to file only (no console output)."""
    # Clear any existing handlers to prevent duplicate output
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Configure file-only logging
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    file_handler.setLevel(logging.DEBUG)

    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)


def validate_and_connect(config: Config) -> "dropbox.Dropbox":
    """Validate config and connect to Dropbox. Returns Dropbox client."""
    from .display import print_error, print_header, print_info, print_success
    from .utils import human_size

    try:
        import dropbox
        from dropbox.exceptions import AuthError
    except ImportError:
        print_error("The 'dropbox' package is required.")
        print_info("Install it with: pip install dropbox")
        sys.exit(1)

    print_header("Configuration")
    print()

    # Validate config
    errors = config.validate()
    if errors:
        for error in errors:
            print_error(error)
        sys.exit(1)

    print_success("Configuration valid")

    # Ensure destination exists
    dest = config.ensure_dest_exists()
    print_success(f"Destination: {dest}")

    # Show disk space
    try:
        usage = shutil.disk_usage(dest)
        print_info(f"Disk: {usage.free / 1e9:.1f} GB free / {usage.total / 1e9:.1f} GB total")
    except OSError:
        pass

    print()
    print_info(f"Threads: {config.max_concurrent_downloads}")
    print_info(f"Timeout: {config.download_timeout}s")
    print_info(f"Max retries: {config.max_retries}")

    # Connect to Dropbox
    print()
    print_info("Connecting to Dropbox...")

    try:
        dbx = dropbox.Dropbox(
            oauth2_access_token=config.access_token,
            timeout=config.download_timeout,
        )
        account = dbx.users_get_current_account()
        print_success(f"Connected: {account.name.display_name} ({account.email})")

        # Show storage usage
        try:
            space = dbx.users_get_space_usage()
            if hasattr(space.allocation, "get_individual"):
                allocated = space.allocation.get_individual().allocated
                print_info(f"Storage: {human_size(space.used)} / {human_size(allocated)}")
        except Exception:
            pass

        return dbx

    except AuthError as e:
        print_error(f"Authentication failed: {e}")
        print_info("Your token may have expired. Generate a new one from the Dropbox App Console.")
        sys.exit(1)
    except Exception as e:
        print_error(f"Connection failed: {e}")
        sys.exit(1)


def configure_filters() -> "FilterOptions":
    """Interactive filter configuration."""
    from .display import Colors, ask_choice, ask_yes_no, print_header, print_success, print_warning
    from .models import FilterOptions
    from .utils import human_size, parse_size

    filters = FilterOptions()

    print_header("Backup Options")
    print()

    # Dependency folders
    print(f"  {Colors.BOLD}Dependency Folders{Colors.RESET}")
    print(f"  {Colors.DIM}Includes: node_modules, venv, .next, dist, build, __pycache__, .git, etc.{Colors.RESET}")
    print()

    filters.skip_dependencies = ask_yes_no("Skip dependency/build folders?", True)
    print_success("Will skip dependency folders" if filters.skip_dependencies else "Will include all folders")
    print()

    # Advanced filters
    if ask_yes_no("Configure advanced filters?", False):
        # Extension filters
        choice = ask_choice(
            "File type filter?",
            ["All file types", "Include only specific extensions", "Exclude specific extensions"],
            0,
        )

        if choice == 1:
            ext_input = input("\n  Enter extensions to include (e.g., jpg,png,pdf): ").strip()
            if ext_input:
                from .filters import parse_extensions
                filters.include_extensions = parse_extensions(ext_input)
                print_success(f"Including only: {', '.join(sorted(filters.include_extensions))}")
        elif choice == 2:
            ext_input = input("\n  Enter extensions to exclude (e.g., log,tmp,bak): ").strip()
            if ext_input:
                from .filters import parse_extensions
                filters.exclude_extensions = parse_extensions(ext_input)
                print_success(f"Excluding: {', '.join(sorted(filters.exclude_extensions))}")

        # Size filters
        print(f"\n  {Colors.BOLD}Size Filters{Colors.RESET} (e.g., 1KB, 10MB, 1GB, or 0 for none)")

        try:
            min_input = input("  Minimum file size: ").strip()
            if min_input and min_input != "0":
                filters.min_size_bytes = parse_size(min_input)
                print_success(f"Minimum size: {human_size(filters.min_size_bytes)}")
        except ValueError:
            print_warning("Invalid size, skipping")

        try:
            max_input = input("  Maximum file size: ").strip()
            if max_input and max_input != "0":
                filters.max_size_bytes = parse_size(max_input)
                print_success(f"Maximum size: {human_size(filters.max_size_bytes)}")
        except ValueError:
            print_warning("Invalid size, skipping")

    print()
    filters.dry_run = ask_yes_no("Perform a dry run (no actual downloads)?", False)
    if filters.dry_run:
        print_warning("DRY RUN MODE ENABLED")

    return filters


def main(config: Config | None = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        config: Optional pre-configured Config. If None, loads from environment.

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    from .display import (
        Colors,
        ask_yes_no,
        print_banner,
        print_error,
        print_header,
        print_info,
        print_summary,
        print_warning,
    )
    from .downloader import run_backup
    from .models import DownloadStats
    from .scanner import scan_dropbox
    from .utils import human_size

    # Load config from environment if not provided
    if config is None:
        config = Config.from_env()

    # Print banner
    print_banner()

    # Check if destination is missing and prompt for it
    if not config.dest_root:
        dest = prompt_for_destination()
        if dest:
            config.dest_root = dest
        else:
            print_error("No destination folder selected. Exiting.")
            return 1

    # Check if token is missing
    if not config.access_token or "PASTE" in config.access_token:
        print_header("Configuration")
        print()
        print_error("DROPBOX_ACCESS_TOKEN not configured!")
        print()
        print_info("To get your access token:")
        print("    1. Go to https://www.dropbox.com/developers/apps")
        print("    2. Create an app (or select existing)")
        print("    3. Generate an access token")
        print("    4. Add it to your .env file")
        print()
        return 1

    # Set up signal handling
    stop_event = Event()
    interrupted = False

    def signal_handler(sig: int, frame: Any) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            stop_event.set()
            print(Colors.SHOW_CURSOR, end="")
            print(f"\n\n  {Colors.YELLOW}⚠{Colors.RESET} Gracefully stopping... please wait.")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Ensure cursor is shown on exit
    atexit.register(lambda: print(Colors.SHOW_CURSOR, end="", flush=True))

    # Validate and connect
    dbx = validate_and_connect(config)

    # Set up logging
    log_file = Path.cwd() / "dropbox_backup.log"
    setup_logging(log_file)
    logger.info("=" * 50)
    logger.info("Backup started")
    print_info(f"Log file: {log_file}")

    # Configure filters
    filters = configure_filters()

    # Show summary before starting
    print_header("Ready to Backup")
    print()
    print(f"    {Colors.BOLD}Source:{Colors.RESET}  Dropbox:{config.root_path or '/'}")
    print(f"    {Colors.BOLD}Dest:{Colors.RESET}    {config.dest_root}")
    print(f"    {Colors.BOLD}Threads:{Colors.RESET} {config.max_concurrent_downloads}")
    if config.max_gb_per_run > 0:
        print(f"    {Colors.BOLD}Limit:{Colors.RESET}   {config.max_gb_per_run:.0f} GB per run")
    print()

    if not ask_yes_no("Start backup?", True):
        print_info("Backup cancelled.")
        return 0

    # Scan Dropbox
    print_header("Scanning")
    files, _, _ = scan_dropbox(dbx, config.root_path, filters, config)

    if not files:
        print_warning("No files to download.")
        return 0

    # Confirm download
    total_size = sum(f.size for f in files)
    if not ask_yes_no(f"Download {len(files):,} files ({human_size(total_size)})?", True):
        print_info("Backup cancelled.")
        return 0

    # Run the backup
    stats = DownloadStats()
    run_backup(dbx, files, filters, stats, stop_event, config)

    # Print summary
    print_summary(stats, interrupted)

    # Log completion
    logger.info(
        "Backup completed: %d downloaded, %d skipped, %d failed",
        stats.files_downloaded,
        stats.files_skipped_exists,
        stats.files_failed,
    )

    return 1 if stats.files_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
