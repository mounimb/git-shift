"""Read-only view of the GitHub CLI's accounts (implemented in Task 11)."""

from __future__ import annotations

from . import paths


def _executable() -> str | None:
    return paths.find_executable("gh")
