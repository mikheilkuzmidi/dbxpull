"""Entry point for running dropbox_backup as a module."""

import sys

from .cli import cli_main

if __name__ == "__main__":
    sys.exit(cli_main())
