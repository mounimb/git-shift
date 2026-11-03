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


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    # env: Python on Windows passes an empty variable to children only through env=
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def write_fake_gh(directory: Path, output: str, stream: str = "stdout", exit_code: int = 0) -> Path:
    """Create a fake `gh` executable that prints `output`, whatever its arguments.

    `stream` is "stdout" or "stderr"; the script then exits with `exit_code`.
    On Windows it is a .cmd script, which paths.find_executable() never picks up, so
    tests inject it by patching gh._executable rather than through PATH.
    """
    if stream not in ("stdout", "stderr"):
        raise ValueError(f"unknown stream {stream!r}")
    data = directory / "gh-output.txt"
    data.write_text(output, encoding="utf-8")
    if os.name == "nt":
        script = directory / "gh.cmd"
        redirect = " 1>&2" if stream == "stderr" else ""
        lines = [f'@type "{data}"{redirect}']
        if exit_code:
            lines.append(f"@exit /b {exit_code}")
        script.write_text("".join(line + "\r\n" for line in lines), encoding="utf-8")
    else:
        script = directory / "gh"
        redirect = " >&2" if stream == "stderr" else ""
        lines = ["#!/bin/sh", f"cat '{data}'{redirect}"]
        if exit_code:
            lines.append(f"exit {exit_code}")
        script.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        script.chmod(0o755)
    return script
