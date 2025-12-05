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
    from .display import print_error, print_header, print_info, print_success, print_warning
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
        # Use refresh token authentication if available (recommended - auto-refreshes)
        if config.has_refresh_token_auth():
            print_info("Using refresh token authentication (auto-refresh enabled)")
            dbx = dropbox.Dropbox(
                app_key=config.app_key,
                app_secret=config.app_secret,
                oauth2_refresh_token=config.refresh_token,
                timeout=config.download_timeout,
            )
        else:
            # Fall back to legacy access token
            print_warning("Using legacy access token (may expire during long backups)")
            print_info("Run 'dropbox-backup auth' to set up auto-refreshing tokens")
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
        if config.has_refresh_token_auth():
            print_info("Your refresh token may be invalid. Run 'dropbox-backup auth' to re-authenticate.")
        else:
            print_info("Your access token has expired. Run 'dropbox-backup auth' to set up auto-refreshing tokens.")
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

    # Check if authentication is configured
    if not config.has_refresh_token_auth() and not config.has_legacy_token_auth():
        print_header("Configuration")
        print()
        print_error("No Dropbox authentication configured!")
        print()
        print_info("Run 'dropbox-backup auth' to set up authentication.")
        print()
        print_info("Or manually configure in your .env file:")
        print("    # Recommended: OAuth with auto-refresh")
        print("    DROPBOX_APP_KEY=your_app_key")
        print("    DROPBOX_APP_SECRET=your_app_secret")
        print("    DROPBOX_REFRESH_TOKEN=your_refresh_token")
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


def run_auth() -> int:
    """
    Run the OAuth flow to obtain a refresh token.

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    from .display import Colors, print_error, print_header, print_info, print_success, print_warning

    try:
        from dropbox import DropboxOAuth2FlowNoRedirect
    except ImportError:
        print_error("The 'dropbox' package is required.")
        print_info("Install it with: pip install dropbox")
        return 1

    print()
    print_header("Dropbox OAuth Setup")
    print()
    print_info("This will help you set up OAuth authentication with auto-refreshing tokens.")
    print_info("You'll need your App Key and App Secret from the Dropbox App Console.")
    print()

    # Load existing config to check for app_key/app_secret
    config = Config.from_env()

    # Get app key
    if config.app_key:
        print_info(f"Found DROPBOX_APP_KEY in environment: {config.app_key[:8]}...")
        use_existing = input(f"  {Colors.CYAN}?{Colors.RESET} Use this app key? [Y/n]: ").strip().lower()
        if use_existing in ("", "y", "yes"):
            app_key = config.app_key
        else:
            app_key = input(f"  {Colors.CYAN}?{Colors.RESET} Enter your App Key: ").strip()
    else:
        print_info("Get your App Key from: https://www.dropbox.com/developers/apps")
        app_key = input(f"  {Colors.CYAN}?{Colors.RESET} Enter your App Key: ").strip()

    if not app_key:
        print_error("App Key is required.")
        return 1

    # Get app secret
    if config.app_secret:
        print_info(f"Found DROPBOX_APP_SECRET in environment: {config.app_secret[:4]}...")
        use_existing = input(f"  {Colors.CYAN}?{Colors.RESET} Use this app secret? [Y/n]: ").strip().lower()
        if use_existing in ("", "y", "yes"):
            app_secret = config.app_secret
        else:
            app_secret = input(f"  {Colors.CYAN}?{Colors.RESET} Enter your App Secret: ").strip()
    else:
        app_secret = input(f"  {Colors.CYAN}?{Colors.RESET} Enter your App Secret: ").strip()

    if not app_secret:
        print_error("App Secret is required.")
        return 1

    print()
    print_info("Starting OAuth flow...")

    # Start OAuth flow with offline access to get refresh token
    auth_flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        app_secret,
        token_access_type="offline",  # This gives us a refresh token
    )

    authorize_url = auth_flow.start()

    print()
    print(f"  {Colors.BOLD}1.{Colors.RESET} Open this URL in your browser:")
    print()
    print(f"     {Colors.CYAN}{authorize_url}{Colors.RESET}")
    print()
    print(f"  {Colors.BOLD}2.{Colors.RESET} Click 'Allow' to authorize the app")
    print(f"  {Colors.BOLD}3.{Colors.RESET} Copy the authorization code")
    print()

    auth_code = input(f"  {Colors.CYAN}?{Colors.RESET} Enter the authorization code: ").strip()

    if not auth_code:
        print_error("Authorization code is required.")
        return 1

    try:
        oauth_result = auth_flow.finish(auth_code)
    except Exception as e:
        print_error(f"Failed to complete OAuth flow: {e}")
        return 1

    refresh_token = oauth_result.refresh_token

    if not refresh_token:
        print_error("No refresh token received. Make sure your app has offline access enabled.")
        return 1

    print()
    print_success("Authentication successful!")
    print()

    # Show the credentials to add to .env
    print_header("Add to your .env file")
    print()
    print(f"  {Colors.GREEN}# Dropbox OAuth (auto-refreshing){Colors.RESET}")
    print(f"  {Colors.BOLD}DROPBOX_APP_KEY{Colors.RESET}=\"{app_key}\"")
    print(f"  {Colors.BOLD}DROPBOX_APP_SECRET{Colors.RESET}=\"{app_secret}\"")
    print(f"  {Colors.BOLD}DROPBOX_REFRESH_TOKEN{Colors.RESET}=\"{refresh_token}\"")
    print()

    # Offer to update .env file automatically
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        print_warning(f"Found existing .env file at {env_path}")
        update_env = input(f"  {Colors.CYAN}?{Colors.RESET} Update .env file automatically? [Y/n]: ").strip().lower()
    else:
        update_env = input(f"  {Colors.CYAN}?{Colors.RESET} Create .env file? [Y/n]: ").strip().lower()

    if update_env in ("", "y", "yes"):
        _update_env_file(env_path, app_key, app_secret, refresh_token)
        print_success(f"Updated {env_path}")
    else:
        print_info("Remember to add the credentials above to your .env file.")

    print()
    print_success("Setup complete! You can now run 'dropbox-backup' to start backing up.")
    print_info("Your tokens will auto-refresh, so backups won't fail due to expiration.")
    print()

    return 0


def _update_env_file(env_path: Path, app_key: str, app_secret: str, refresh_token: str) -> None:
    """Update or create .env file with OAuth credentials."""
    lines: list[str] = []
    keys_to_update = {
        "DROPBOX_APP_KEY": app_key,
        "DROPBOX_APP_SECRET": app_secret,
        "DROPBOX_REFRESH_TOKEN": refresh_token,
    }
    keys_found: set[str] = set()

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # Check if this line sets one of our keys
            updated = False
            for key, value in keys_to_update.items():
                if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                    lines.append(f'{key}="{value}"')
                    keys_found.add(key)
                    updated = True
                    break
            if not updated:
                lines.append(line)

    # Add any missing keys
    missing_keys = set(keys_to_update.keys()) - keys_found
    if missing_keys:
        if lines and lines[-1].strip():  # Add blank line if file doesn't end with one
            lines.append("")
        lines.append("# Dropbox OAuth (auto-refreshing)")
        for key in ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]:
            if key in missing_keys:
                lines.append(f'{key}="{keys_to_update[key]}"')

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cli_main() -> int:
    """CLI entry point with argument parsing."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dropbox-backup",
        description="High-performance parallel backup tool for Dropbox",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Auth command
    subparsers.add_parser(
        "auth",
        help="Set up OAuth authentication with auto-refreshing tokens",
    )

    # Backup is the default (no subcommand needed)
    subparsers.add_parser(
        "backup",
        help="Run backup (default if no command specified)",
    )

    args = parser.parse_args()

    if args.command == "auth":
        return run_auth()
    else:
        # Default to backup
        return main()


if __name__ == "__main__":
    sys.exit(cli_main())
