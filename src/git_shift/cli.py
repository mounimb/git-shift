"""Command-line interface for git-shift."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-shift",
        description="Folder-based git identities, with a guard against wrong-account commits.",
    )
    parser.add_argument("--version", action="version", version=f"git-shift {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return 0
