"""Adaptive rate limiter for Dropbox API requests."""

import time
from threading import Lock


class AdaptiveRateLimiter:
    """
    Adaptive rate limiter with dynamic adjustment based on API responses.

    Features:
    - Automatically increases delay when rate limits are hit
    - Gradually decreases delay after successful requests
    - Thread-safe for concurrent downloads
    """

    def __init__(
        self,
        initial_delay: float = 0.1,
        max_delay: float = 5.0,
        window_seconds: float = 60.0,
        threshold: int = 3,
    ):
        """
        Initialize the rate limiter.

        Args:
            initial_delay: Starting delay between requests (seconds)
            max_delay: Maximum delay between requests (seconds)
            window_seconds: Time window for tracking rate limit hits
            threshold: Number of hits in window before aggressive throttling
        """
        self.current_delay = initial_delay
        self.min_delay = initial_delay
        self.max_delay = max_delay
        self.window_seconds = window_seconds
        self.threshold = threshold

        self.lock = Lock()
        self.last_request_time = 0.0
        self.rate_limit_times: list[float] = []
        self.consecutive_successes = 0

    def wait(self) -> None:
        """Wait for the appropriate delay before the next request."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request_time

            if elapsed < self.current_delay:
                time.sleep(self.current_delay - elapsed)

            self.last_request_time = time.time()

    def record_success(self) -> None:
        """Record a successful request (may reduce delay)."""
        with self.lock:
            self.consecutive_successes += 1

            # After 20 consecutive successes, try reducing delay
            if self.consecutive_successes >= 20:
                self.current_delay = max(self.min_delay, self.current_delay * 0.9)
                self.consecutive_successes = 0

    def record_rate_limit(self) -> None:
        """Record a rate limit hit (increases delay)."""
        with self.lock:
            now = time.time()
            self.rate_limit_times.append(now)
            self.consecutive_successes = 0

            # Clean up old entries
            self.rate_limit_times = [
                t for t in self.rate_limit_times
                if now - t < self.window_seconds
            ]

            # Increase delay based on frequency of hits
            if len(self.rate_limit_times) >= self.threshold:
                # Multiple hits in window: aggressive throttle
                self.current_delay = min(self.max_delay, self.current_delay * 2)
            else:
                # Single hit: moderate increase
                self.current_delay = min(self.max_delay, self.current_delay * 1.5)

    @property
    def is_throttled(self) -> bool:
        """Check if currently in throttled state."""
        return self.current_delay > self.min_delay * 2

    @property
    def delay(self) -> float:
        """Get current delay value."""
        with self.lock:
            return self.current_delay

    def reset(self) -> None:
        """Reset the rate limiter to initial state."""
        with self.lock:
            self.current_delay = self.min_delay
            self.last_request_time = 0.0
            self.rate_limit_times = []
            self.consecutive_successes = 0
