"""Terminal display and UI components for Dropbox Backup."""

import sys
from datetime import datetime
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

from .utils import (
    get_terminal_width,
    human_size,
    human_speed,
    human_time,
    is_tty,
    truncate_path,
)

if TYPE_CHECKING:
    from .models import DownloadStats
    from .rate_limiter import AdaptiveRateLimiter


class Colors:
    """ANSI color codes for terminal output."""

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"
    CLEAR_LINE = "\033[2K"

    _disabled = False

    @classmethod
    def disable(cls) -> None:
        """Disable all color codes (for non-TTY output)."""
        if cls._disabled:
            return
        cls._disabled = True
        for attr in dir(cls):
            if not attr.startswith("_") and attr.isupper():
                setattr(cls, attr, "")

    @classmethod
    def init(cls) -> None:
        """Initialize colors based on terminal capability."""
        if not is_tty():
            cls.disable()


# Initialize colors on module load
Colors.init()


def make_bar(percent: float, width: int = 30) -> str:
    """Create a simple progress bar string."""
    percent = max(0, min(100, percent))
    filled = int((percent / 100) * width)
    return "█" * filled + "░" * (width - filled)


def print_banner() -> None:
    """Print the application banner."""
    C = Colors.CYAN
    W = Colors.WHITE
    B = Colors.BOLD
    D = Colors.DIM
    R = Colors.RESET

    print()
    print(f"{C}╔══════════════════════════════════════════════════════════════════════╗{R}")
    print(f"{C}║{R}{B}{W}                          DROPBOX BACKUP                              {R}{C}║{R}")
    print(f"{C}╠══════════════════════════════════════════════════════════════════════╣{R}")
    print(f"{C}║{R} {D}Parallel Downloads   •  Smart Rate Limiting  •   Exponential Backoff{R} {C}║{R}")
    print(f"{C}╚══════════════════════════════════════════════════════════════════════╝{R}")
    print()


def print_header(text: str) -> None:
    """Print a section header."""
    width = min(get_terminal_width() - 4, 76)
    line_len = max(0, width - len(text) - 5)
    print(f"\n{Colors.MAGENTA}{Colors.BOLD}{'─' * 3} {text} {'─' * line_len}{Colors.RESET}")


def print_success(text: str) -> None:
    """Print a success message."""
    print(f"  {Colors.GREEN}✓{Colors.RESET} {text}")


def print_error(text: str) -> None:
    """Print an error message."""
    print(f"  {Colors.RED}✗{Colors.RESET} {text}")


def print_warning(text: str) -> None:
    """Print a warning message."""
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {text}")


def print_info(text: str) -> None:
    """Print an info message."""
    print(f"  {Colors.CYAN}ℹ{Colors.RESET} {text}")


def ask_yes_no(question: str, default: bool = True) -> bool:
    """Ask a yes/no question. Returns boolean."""
    hint = "[Y/n]" if default else "[y/N]"

    while True:
        try:
            ans = input(f"  {Colors.CYAN}?{Colors.RESET} {question} {Colors.DIM}{hint}{Colors.RESET}: ")
            ans = ans.strip().lower()
        except EOFError:
            return default
        except KeyboardInterrupt:
            print()
            return default

        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False

        print_warning("Please enter 'y' or 'n'.")


def ask_choice(question: str, choices: list[str], default: int = 0) -> int:
    """Ask user to choose from a list. Returns index."""
    print(f"\n  {Colors.CYAN}?{Colors.RESET} {question}\n")

    for i, choice in enumerate(choices):
        marker = f"{Colors.GREEN}●{Colors.RESET}" if i == default else f"{Colors.DIM}○{Colors.RESET}"
        tag = f" {Colors.DIM}(default){Colors.RESET}" if i == default else ""
        print(f"    {marker} [{i + 1}] {choice}{tag}")

    while True:
        try:
            ans = input(f"\n  Choice [1-{len(choices)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return default

        if not ans:
            return default

        try:
            idx = int(ans) - 1
            if 0 <= idx < len(choices):
                return idx
        except ValueError:
            pass

        print_warning(f"Enter a number from 1 to {len(choices)}.")


def print_summary(stats: "DownloadStats", interrupted: bool = False) -> None:
    """Print the backup summary."""
    print("\n" * 3)

    if interrupted:
        print_header(f"{Colors.YELLOW}BACKUP INTERRUPTED{Colors.RESET}")
    else:
        print_header(f"{Colors.GREEN}BACKUP COMPLETE{Colors.RESET}")

    print()
    elapsed = human_time(stats.elapsed_seconds)
    avg_speed = human_speed(stats.speed_bps) if stats.speed_bps > 0 else "N/A"

    print(f"  {Colors.BOLD}Performance{Colors.RESET}")
    print(f"    Completed:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"    Duration:     {elapsed}")
    print(f"    Avg Speed:    {avg_speed}")
    print()

    print(f"  {Colors.BOLD}Files{Colors.RESET}")
    print(f"    {Colors.GREEN}Downloaded:{Colors.RESET}   {stats.files_downloaded:,} ({human_size(stats.bytes_downloaded)})")
    print(f"    {Colors.BLUE}Already had:{Colors.RESET}  {stats.files_skipped_exists:,} ({human_size(stats.bytes_skipped)})")
    print(f"    {Colors.YELLOW}Filtered:{Colors.RESET}     {stats.files_skipped_filter:,}")
    print(f"    {Colors.MAGENTA}Dependencies:{Colors.RESET} {stats.files_skipped_dependency:,}")

    if stats.files_failed > 0:
        print(f"    {Colors.RED}Failed:{Colors.RESET}       {stats.files_failed:,}")
    print()

    if stats.rate_limit_hits > 0:
        print(f"  {Colors.BOLD}Rate Limiting{Colors.RESET}")
        print(f"    Hits: {stats.rate_limit_hits}  Retries: {stats.retries_total}")
        print()

    print(f"  {'─' * 60}")

    if not interrupted and stats.files_failed == 0:
        print(f"  {Colors.GREEN}✓{Colors.RESET} {Colors.BOLD}All done!{Colors.RESET} Files safely backed up.")
    elif interrupted:
        print(f"  {Colors.YELLOW}⚠{Colors.RESET} Interrupted. Run again to continue.")
    else:
        print(f"  {Colors.YELLOW}⚠{Colors.RESET} Completed with {stats.files_failed} failures. Check log.")

    print(f"  {'─' * 60}")
    print()


class ProgressDisplay:
    """Real-time progress display with fixed slot layout.

    Displays exactly N+1 lines:
    - 1 overall progress bar
    - N download slot bars (matching concurrent download count)

    Uses in-place terminal updates for a clean, non-scrolling display.
    """

    REFRESH_INTERVAL = 0.5  # Seconds between updates (reduces CPU load)

    def __init__(
        self,
        stats: "DownloadStats",
        rate_limiter: "AdaptiveRateLimiter",
        max_slots: int = 6,
    ):
        self.stats = stats
        self.rate_limiter = rate_limiter
        self.max_slots = max_slots
        # Lines: 1 overall + 1 stats + max_slots download bars
        self.total_lines = 2 + max_slots

        self.lock = Lock()
        self.running = False
        self.thread: Thread | None = None
        self.stop_event = Event()
        self._initialized = False

    def start(self) -> None:
        """Start the progress display."""
        self._initialized = False
        self.running = True
        self.stop_event.clear()
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the progress display."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.running = False
        self._render()  # Final render

    def _run(self) -> None:
        """Background update loop."""
        while not self.stop_event.is_set():
            self._render()
            # Use Event.wait for interruptible sleep
            self.stop_event.wait(self.REFRESH_INTERVAL)

    def _render(self) -> None:
        """Render the progress display in-place."""
        with self.lock:
            # Build all display lines first
            output_lines: list[str] = []

            # === Line 1: Overall progress bar ===
            total = self.stats.bytes_total
            done = self.stats.bytes_downloaded + self.stats.bytes_skipped
            percent = (done / total * 100) if total > 0 else 0
            bar = make_bar(percent, width=40)

            speed = human_speed(self.stats.speed_bps)
            eta = human_time(self.stats.eta_seconds)
            files_done = self.stats.files_downloaded + self.stats.files_skipped_exists

            output_lines.append(
                f"  [{Colors.CYAN}{bar}{Colors.RESET}] {percent:5.1f}%  "
                f"{speed:>10}  ETA: {eta:<10}  "
                f"Files: {files_done:,}/{self.stats.files_total:,}"
            )

            # === Line 2: Status line ===
            throttle = f" {Colors.YELLOW}[Throttled]{Colors.RESET}" if self.rate_limiter.is_throttled else ""
            output_lines.append(
                f"  {Colors.DIM}Active: {self.stats.active_count}/{self.max_slots}{throttle}  "
                f"Downloaded: {Colors.GREEN}{human_size(self.stats.bytes_downloaded)}{Colors.RESET} / "
                f"{human_size(total)}{Colors.RESET}"
            )

            # === Lines 3-N: Download slot bars ===
            active = self.stats.get_active_downloads()
            for i in range(self.max_slots):
                if i < len(active):
                    dl = active[i]
                    dl_percent = dl.progress_percent
                    dl_bar = make_bar(dl_percent, width=20)
                    size_done = human_size(dl.downloaded_bytes, 1)
                    size_total = human_size(dl.total_bytes, 1)
                    name = truncate_path(dl.path, 35)
                    output_lines.append(
                        f"  {Colors.DIM}#{i+1}{Colors.RESET} "
                        f"[{Colors.BLUE}{dl_bar}{Colors.RESET}] {dl_percent:5.1f}% "
                        f"{size_done:>8}/{size_total:<8} {name}"
                    )
                else:
                    output_lines.append(
                        f"  {Colors.DIM}#{i+1} "
                        f"[{'░' * 20}]   --.-% "
                        f"{'':>17} (waiting){Colors.RESET}"
                    )

            # === Write to terminal ===
            # On first render, print blank lines to reserve space
            if not self._initialized:
                sys.stdout.write("\n" * self.total_lines)
                self._initialized = True

            # Move cursor up to overwrite previous output
            sys.stdout.write(f"\033[{self.total_lines}A")

            # Write each line, clearing to end of line
            for line in output_lines:
                # Pad line to clear any leftover characters
                sys.stdout.write(f"\r\033[K{line}\n")

            sys.stdout.flush()
