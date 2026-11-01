"""Read-only view of the GitHub CLI's accounts (implemented in Task 11)."""

import shutil


def _executable() -> str | None:
    return shutil.which("gh")
