"""Thin wrapper around `git config`. git-shift never parses config files itself."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .errors import GitShiftError


@dataclass(frozen=True)
class Origin:
    kind: str  # "file", "command line", "blob", "standard input"
    path: str  # absolute config file path when kind == "file", else ""
    value: str


def run_git(
    *args: str, cwd: str | Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    exe = paths.find_executable("git")
    if exe is None:
        raise GitShiftError("git is not installed or not on PATH")
    try:
        proc = subprocess.run(
            [exe, *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,  # inside hooks, stdin belongs to git (pre-push refs)
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GitShiftError(f"cannot run git: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitShiftError(f"`git {' '.join(args)}` failed: {proc.stderr.strip()}")
    return proc


def _where(scope: str | None, file: str | Path | None) -> list[str]:
    if file is not None:
        return ["--file", str(file)]
    if scope is not None:
        return [f"--{scope}"]
    return []


def _read(args: list[str], cwd) -> str | None:
    """stdout of a `git config` read; None when the key is missing (exit code 1).

    git also exits 1 for a malformed key name, so keys must be constants, never user input.
    """
    proc = run_git("config", *args, cwd=cwd, check=False)
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitShiftError(f"git config failed: {proc.stderr.strip()}")
    return proc.stdout


def get(key: str, *, scope=None, file=None, cwd=None) -> str | None:
    out = _read([*_where(scope, file), "-z", "--get", key], cwd)
    return None if out is None else out.split("\0")[0]


def get_all(key: str, *, scope=None, file=None, cwd=None) -> list[str]:
    out = _read([*_where(scope, file), "-z", "--get-all", key], cwd)
    return [] if out is None else out.split("\0")[:-1]


def get_with_origin(key: str, *, cwd=None) -> Origin | None:
    """Effective value of `key` in `cwd` and the place it comes from."""
    out = _read(["--show-origin", "-z", "--get", key], cwd)
    if out is None:
        return None
    origin, value = out.split("\0")[:2]
    kind, _, path = origin.partition(":")
    if kind == "file" and path and not os.path.isabs(path):
        top = run_git("rev-parse", "--show-toplevel", cwd=cwd, check=False).stdout.strip()
        path = paths.to_git_path(os.path.join(top or os.fspath(cwd or os.getcwd()), path))
    return Origin(kind=kind, path=path, value=value)


def list_entries(*, scope=None, file=None, cwd=None) -> list[tuple[str, str]]:
    """All (key, value) pairs in file order; --file and --global do not follow includes.

    Keys come back lowercased except subsections; a key without a value reads as "true".
    A config file that does not exist yet gives [].
    """
    if file is not None and not Path(file).exists():
        return []
    if scope == "global" and not any(path.is_file() for path in paths.global_config_files()):
        return []
    proc = run_git("config", *_where(scope, file), "-z", "--list", cwd=cwd)
    entries = []
    for record in proc.stdout.split("\0"):
        if record:
            key, sep, value = record.partition("\n")
            entries.append((key, value if sep else "true"))
    return entries


def set_value(
    key: str, value: str, *, scope=None, file=None, cwd=None, replace_all: bool = False
) -> None:
    """Set one value. git refuses (exit 5) when the key has several, unless replace_all."""
    if file is not None:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
    flags = ["--replace-all"] if replace_all else []
    run_git("config", *_where(scope, file), *flags, key, value, cwd=cwd)


def add_value(key: str, value: str, *, scope=None, file=None, cwd=None) -> None:
    if file is not None:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
    run_git("config", *_where(scope, file), "--add", key, value, cwd=cwd)


def unset(key: str, *, scope=None, file=None, cwd=None) -> None:
    if file is not None and not Path(file).exists():
        return
    proc = run_git("config", *_where(scope, file), "--unset-all", key, cwd=cwd, check=False)
    if proc.returncode not in (0, 5):  # 5 = key was not set
        raise GitShiftError(f"could not unset {key}: {proc.stderr.strip()}")


def git_version() -> tuple[int, int]:
    out = run_git("--version").stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    if match is None:
        raise GitShiftError(f"cannot parse git version from {out!r}")
    return int(match.group(1)), int(match.group(2))
