"""Tests for dropbox_backup package."""


from dropbox_backup.config import DEFAULT_SKIP_DIRS, Config
from dropbox_backup.filters import get_file_category, parse_extensions, should_skip_file
from dropbox_backup.models import ActiveDownload, DownloadStats, FilterOptions
from dropbox_backup.rate_limiter import AdaptiveRateLimiter
from dropbox_backup.utils import (
    exponential_backoff_with_jitter,
    human_size,
    human_speed,
    human_time,
    normalize_dropbox_path,
    parse_size,
    truncate_path,
)


class TestUtils:
    """Tests for utility functions."""

    def test_human_size_bytes(self):
        assert human_size(0) == "0.00 B"
        assert human_size(100) == "100.00 B"
        assert human_size(1023) == "1023.00 B"

    def test_human_size_kilobytes(self):
        assert human_size(1024) == "1.00 KB"
        assert human_size(1536) == "1.50 KB"

    def test_human_size_megabytes(self):
        assert human_size(1024 * 1024) == "1.00 MB"
        assert human_size(1024 * 1024 * 1.5) == "1.50 MB"

    def test_human_size_gigabytes(self):
        assert human_size(1024 ** 3) == "1.00 GB"

    def test_human_time_seconds(self):
        assert human_time(0) == "0s"
        assert human_time(30) == "30s"
        assert human_time(59) == "59s"

    def test_human_time_minutes(self):
        assert human_time(60) == "1m 0s"
        assert human_time(90) == "1m 30s"
        assert human_time(3599) == "59m 59s"

    def test_human_time_hours(self):
        assert human_time(3600) == "1h 0m"
        assert human_time(3660) == "1h 1m"
        assert human_time(7200) == "2h 0m"

    def test_human_time_none(self):
        assert human_time(None) == "calculating..."

    def test_human_time_negative(self):
        assert human_time(-1) == "unknown"

    def test_human_speed(self):
        assert human_speed(100) == "100 B/s"
        assert human_speed(1024) == "1.0 KB/s"
        assert human_speed(1024 * 1024) == "1.00 MB/s"

    def test_truncate_path_short(self):
        path = "/short/path"
        assert truncate_path(path, 40) == path

    def test_truncate_path_long(self):
        path = "/very/long/path/that/should/be/truncated/file.txt"
        result = truncate_path(path, 20)
        assert len(result) == 20
        assert result.startswith("...")

    def test_parse_size_bytes(self):
        assert parse_size("100") == 100
        assert parse_size("100B") == 100
        assert parse_size("100 B") == 100

    def test_parse_size_kilobytes(self):
        assert parse_size("1KB") == 1024
        assert parse_size("1K") == 1024
        assert parse_size("1.5KB") == 1536

    def test_parse_size_megabytes(self):
        assert parse_size("1MB") == 1024 * 1024
        assert parse_size("1M") == 1024 * 1024

    def test_parse_size_gigabytes(self):
        assert parse_size("1GB") == 1024 ** 3
        assert parse_size("1G") == 1024 ** 3

    def test_normalize_dropbox_path_empty(self):
        assert normalize_dropbox_path("") == ""
        assert normalize_dropbox_path("/") == ""

    def test_normalize_dropbox_path_with_slash(self):
        assert normalize_dropbox_path("/folder") == "/folder"
        assert normalize_dropbox_path("/folder/") == "/folder"

    def test_normalize_dropbox_path_without_slash(self):
        assert normalize_dropbox_path("folder") == "/folder"
        assert normalize_dropbox_path("folder/subfolder") == "/folder/subfolder"

    def test_exponential_backoff_with_jitter(self):
        # Test that it returns values within expected range
        for attempt in range(5):
            delay = exponential_backoff_with_jitter(attempt, base=1.0, factor=2.0, max_delay=60.0)
            max_possible = min(60.0, 1.0 * (2.0 ** attempt))
            assert 0 <= delay <= max_possible


class TestConfig:
    """Tests for configuration."""

    def test_default_config(self):
        config = Config()
        assert config.access_token == ""
        assert config.max_concurrent_downloads == 6
        assert config.max_retries == 5

    def test_config_validation_missing_auth(self):
        config = Config()
        errors = config.validate()
        assert any("authentication" in e.lower() for e in errors)

    def test_config_validation_placeholder_token(self):
        config = Config(access_token="PASTE_YOUR_TOKEN_HERE")
        errors = config.validate()
        assert any("placeholder" in e.lower() or "authentication" in e.lower() for e in errors)

    def test_config_validation_missing_dest(self):
        config = Config(access_token="valid_token")
        errors = config.validate()
        assert "Destination directory is required" in errors

    def test_config_has_refresh_token_auth(self):
        config = Config(app_key="key", app_secret="secret", refresh_token="token")
        assert config.has_refresh_token_auth() is True
        assert config.has_legacy_token_auth() is False

    def test_config_has_legacy_token_auth(self):
        config = Config(access_token="valid_token")
        assert config.has_legacy_token_auth() is True
        assert config.has_refresh_token_auth() is False

    def test_default_skip_dirs(self):
        assert "node_modules" in DEFAULT_SKIP_DIRS
        assert "venv" in DEFAULT_SKIP_DIRS
        assert ".git" in DEFAULT_SKIP_DIRS
        assert "__pycache__" in DEFAULT_SKIP_DIRS


class TestFilterOptions:
    """Tests for filter options."""

    def test_default_options(self):
        opts = FilterOptions()
        assert opts.skip_dependencies is True
        assert opts.dry_run is False
        assert len(opts.include_extensions) == 0
        assert len(opts.exclude_extensions) == 0

    def test_extension_normalization(self):
        opts = FilterOptions(
            include_extensions={".JPG", "PNG", ".gif"},
            exclude_extensions={".LOG", "tmp"},
        )
        assert opts.include_extensions == {"jpg", "png", "gif"}
        assert opts.exclude_extensions == {"log", "tmp"}

    def test_parse_extensions(self):
        assert parse_extensions("") == set()
        assert parse_extensions("jpg,png") == {"jpg", "png"}
        assert parse_extensions(".JPG, .PNG, gif") == {"jpg", "png", "gif"}
        assert parse_extensions("  pdf  ,  doc  ") == {"pdf", "doc"}


class TestFileCategory:
    """Tests for file categorization."""

    def test_document_category(self):
        assert get_file_category("file.pdf") == "document"
        assert get_file_category("file.docx") == "document"
        assert get_file_category("file.xlsx") == "document"

    def test_image_category(self):
        assert get_file_category("photo.jpg") == "image"
        assert get_file_category("photo.PNG") == "image"
        assert get_file_category("photo.svg") == "image"

    def test_video_category(self):
        assert get_file_category("video.mp4") == "video"
        assert get_file_category("video.mkv") == "video"

    def test_audio_category(self):
        assert get_file_category("music.mp3") == "audio"
        assert get_file_category("music.flac") == "audio"

    def test_code_category(self):
        assert get_file_category("script.py") == "code"
        assert get_file_category("app.js") == "code"
        assert get_file_category("main.go") == "code"

    def test_archive_category(self):
        assert get_file_category("archive.zip") == "archive"
        assert get_file_category("backup.tar.gz") == "archive"  # .gz is also archive

    def test_other_category(self):
        assert get_file_category("file.xyz") == "other"
        assert get_file_category("noextension") == "other"


class TestDownloadStats:
    """Tests for download statistics."""

    def test_initial_state(self):
        stats = DownloadStats()
        assert stats.files_total == 0
        assert stats.files_downloaded == 0
        assert stats.active_count == 0

    def test_increment(self):
        stats = DownloadStats()
        stats.increment("files_downloaded")
        assert stats.files_downloaded == 1
        stats.increment("bytes_downloaded", 1024)
        assert stats.bytes_downloaded == 1024

    def test_active_downloads(self):
        stats = DownloadStats()
        slot1 = stats.start_download("/path/file1.txt", 1000)
        stats.start_download("/path/file2.txt", 2000)  # slot2 unused, just need side effect

        assert stats.active_count == 2
        active = stats.get_active_downloads()
        assert len(active) == 2

        stats.update_download(slot1, 500)
        active = stats.get_active_downloads()
        assert active[0].downloaded_bytes == 500

        stats.finish_download(slot1)
        assert stats.active_count == 1

    def test_speed_calculation(self):
        stats = DownloadStats()
        stats.bytes_downloaded = 1024 * 1024  # 1 MB
        # Manually set start time to 1 second ago
        import time
        stats.start_time = time.time() - 1.0

        # Speed should be approximately 1 MB/s
        assert 900000 < stats.speed_bps < 1100000

    def test_files_processed(self):
        stats = DownloadStats()
        stats.files_downloaded = 10
        stats.files_skipped_exists = 5
        stats.files_skipped_filter = 3
        stats.files_failed = 2

        assert stats.files_processed == 20


class TestActiveDownload:
    """Tests for active download tracking."""

    def test_progress_percent(self):
        dl = ActiveDownload(slot=0, path="/test", total_bytes=1000, downloaded_bytes=500)
        assert dl.progress_percent == 50.0

    def test_progress_percent_zero_total(self):
        dl = ActiveDownload(slot=0, path="/test", total_bytes=0, downloaded_bytes=0)
        assert dl.progress_percent == 0.0


class TestRateLimiter:
    """Tests for adaptive rate limiter."""

    def test_initial_state(self):
        limiter = AdaptiveRateLimiter(initial_delay=0.1)
        assert limiter.delay == 0.1
        assert limiter.is_throttled is False

    def test_record_success(self):
        limiter = AdaptiveRateLimiter(initial_delay=0.5)
        limiter.current_delay = 1.0  # Simulate throttled state

        # After 20 successes, delay should decrease
        for _ in range(20):
            limiter.record_success()

        assert limiter.current_delay < 1.0

    def test_record_rate_limit(self):
        limiter = AdaptiveRateLimiter(initial_delay=0.1)
        initial_delay = limiter.current_delay

        limiter.record_rate_limit()

        assert limiter.current_delay > initial_delay

    def test_is_throttled(self):
        limiter = AdaptiveRateLimiter(initial_delay=0.1)
        assert limiter.is_throttled is False

        limiter.current_delay = 0.5  # More than 2x initial
        assert limiter.is_throttled is True

    def test_reset(self):
        limiter = AdaptiveRateLimiter(initial_delay=0.1)
        limiter.record_rate_limit()
        limiter.record_rate_limit()

        limiter.reset()

        assert limiter.delay == 0.1
        assert limiter.consecutive_successes == 0


class MockFileMetadata:
    """Mock Dropbox FileMetadata for testing."""

    def __init__(self, path: str, size: int = 1000):
        self.path_lower = path.lower()
        self.path_display = path
        self.name = path.split("/")[-1]
        self.size = size


class TestShouldSkipFile:
    """Tests for file skip logic."""

    def test_skip_node_modules(self):
        entry = MockFileMetadata("/project/node_modules/package/index.js")
        filters = FilterOptions(skip_dependencies=True)

        should_skip, reason = should_skip_file(entry, filters)

        assert should_skip is True
        assert reason == "dependency"

    def test_skip_venv(self):
        entry = MockFileMetadata("/project/venv/lib/python3.10/site-packages/module.py")
        filters = FilterOptions(skip_dependencies=True)

        should_skip, reason = should_skip_file(entry, filters)

        assert should_skip is True
        assert reason == "dependency"

    def test_dont_skip_regular_file(self):
        entry = MockFileMetadata("/project/src/main.py")
        filters = FilterOptions(skip_dependencies=True)

        should_skip, reason = should_skip_file(entry, filters)

        assert should_skip is False
        assert reason == ""

    def test_include_extensions_filter(self):
        filters = FilterOptions(include_extensions={"py", "txt"})

        py_file = MockFileMetadata("/file.py")
        js_file = MockFileMetadata("/file.js")

        assert should_skip_file(py_file, filters) == (False, "")
        assert should_skip_file(js_file, filters) == (True, "extension")

    def test_exclude_extensions_filter(self):
        filters = FilterOptions(exclude_extensions={"log", "tmp"})

        log_file = MockFileMetadata("/file.log")
        py_file = MockFileMetadata("/file.py")

        assert should_skip_file(log_file, filters) == (True, "extension")
        assert should_skip_file(py_file, filters) == (False, "")

    def test_min_size_filter(self):
        filters = FilterOptions(min_size_bytes=1000)

        small_file = MockFileMetadata("/small.txt", size=500)
        large_file = MockFileMetadata("/large.txt", size=2000)

        assert should_skip_file(small_file, filters) == (True, "size")
        assert should_skip_file(large_file, filters) == (False, "")

    def test_max_size_filter(self):
        filters = FilterOptions(max_size_bytes=1000)

        small_file = MockFileMetadata("/small.txt", size=500)
        large_file = MockFileMetadata("/large.txt", size=2000)

        assert should_skip_file(small_file, filters) == (False, "")
        assert should_skip_file(large_file, filters) == (True, "size")
