"""Entry point for running dbxpull as a module."""

import sys

from .cli import cli_main

if __name__ == "__main__":
    sys.exit(cli_main())
