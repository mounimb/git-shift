"""Shared test helpers (imported by test modules)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GH_STATUS_SAMPLE = """github.com
  ✓ Logged in to github.com account alice (keyring)
  - Active account: true
  - Git operations protocol: https

  ✓ Logged in to github.com account bob (keyring)
  - Active account: false
  - Git operations protocol: https

  X Failed to log in to github.com account carol (default)
  - Active account: false
  - The token in default is invalid.
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def write_fake_gh(directory: Path, output: str) -> Path:
    """Create a fake `gh` executable that prints `output`, whatever its arguments."""
    data = directory / "gh-output.txt"
    data.write_text(output, encoding="utf-8")
    if os.name == "nt":
        script = directory / "gh.cmd"
        script.write_text(f'@type "{data}"\r\n', encoding="utf-8")
    else:
        script = directory / "gh"
        script.write_text(f"#!/bin/sh\ncat '{data}'\n", encoding="utf-8")
        script.chmod(0o755)
    return script
