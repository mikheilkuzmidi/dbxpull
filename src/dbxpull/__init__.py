"""
dbxpull - High-performance parallel backup tool for Dropbox.

Features:
- Parallel downloads with configurable concurrency
- Smart rate limiting with exponential backoff
- Beautiful terminal progress display
- Automatic dependency folder filtering
- Resume capability for interrupted backups
"""

__version__ = "1.0.0"
__author__ = "Mikheil Kuzmidi"
__email__ = ""

from .config import Config
from .downloader import Downloader
from .models import ActiveDownload, DownloadStats, FilterOptions
from .scanner import scan_dropbox

__all__ = [
    "DownloadStats",
    "FilterOptions",
    "ActiveDownload",
    "Downloader",
    "scan_dropbox",
    "Config",
    "__version__",
]
