"""Thin wrapper around `git config`. git-shift never parses config files itself."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .errors import GitCommandError, GitShiftError

# What tells git which repository it is in: a read without them finds none
REPOSITORY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)
_outside: tuple[str, dict[str, str | None]] | None = None  # set by outside_repositories()


@dataclass(frozen=True)
class Origin:
    kind: str  # "file", "command line", "blob", "standard input"
    path: str  # absolute config file path when kind == "file", else ""
    value: str


@contextmanager
def outside_repositories(*, or_in_place: bool = False) -> Iterator[None]:
    """Meanwhile, git calls made without cwd run outside any repository: from a new empty
    dir whose parent is a ceiling, without the variables that name a repository. git then
    reads no .git/config, so a broken one in the current directory cannot fail them, and
    [includeIf] gitdir and onbranch are false while plain [include] still applies.
    realpath: a Windows short name in the temp path would not match the ceiling.
    or_in_place: when that dir cannot be made (TMP not writable), run where we are."""
    global _outside
    if _outside is not None:  # already outside
        yield
        return
    try:
        directory = os.path.realpath(tempfile.mkdtemp(prefix="git-shift-"))
    except OSError:
        if not or_in_place:
            raise
        yield
        return
    env: dict[str, str | None] = dict.fromkeys(REPOSITORY_ENV)
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(directory)
    _outside = (directory, env)
    try:
        yield
    finally:
        _outside = None
        shutil.rmtree(directory, ignore_errors=True)


def _text(data: bytes) -> str:
    """Output as text mode decodes it: UTF-8, then universal newlines."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def run_git(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = True,
    env: dict[str, str | None] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git. env: variables to set for this call, on top of the current environment;
    None removes one. input: what git reads on stdin, sent as UTF-8; without it, stdin is
    empty (inside hooks, stdin belongs to git: the pre-push refs)."""
    exe = paths.find_executable("git")
    if exe is None:
        raise GitShiftError("git is not installed or not on PATH")
    if cwd is None and _outside is not None:
        cwd, outside_env = _outside
        env = {**outside_env, **(env or {})}
    child_env = None
    if env:
        child_env = {**os.environ, **{k: v for k, v in env.items() if v is not None}}
        for name in (k for k, v in env.items() if v is None):
            child_env.pop(name, None)
    try:
        if input is None:
            proc = subprocess.run(
                [exe, *args],
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        else:  # as bytes: text mode would send each "\n" as "\r\n" on Windows
            raw = subprocess.run(
                [exe, *args],
                cwd=cwd,
                env=child_env,
                input=input.encode("utf-8"),
                capture_output=True,
            )
            proc = subprocess.CompletedProcess(
                raw.args, raw.returncode, _text(raw.stdout), _text(raw.stderr)
            )
    except OSError as exc:
        raise GitShiftError(f"cannot run git: {exc}") from exc
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise GitCommandError(f"`git {' '.join(args)}` failed: {stderr}", stderr)
    return proc


def _where(scope: str | None, file: str | Path | None) -> list[str]:
    if file is not None:
        return ["--file", str(file)]
    if scope is not None:
        return [f"--{scope}"]
    return []


def _read(args: list[str], cwd, env=None) -> str | None:
    """stdout of a `git config` read; None when the key is missing (exit code 1).

    git also exits 1 for a malformed key name, so keys must be constants, never user input.
    """
    proc = run_git("config", *args, cwd=cwd, check=False, env=env)
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise GitCommandError(f"git config failed: {stderr}", stderr)
    return proc.stdout


def get(key: str, *, scope=None, file=None, cwd=None, env=None) -> str | None:
    out = _read([*_where(scope, file), "-z", "--get", key], cwd, env)
    return None if out is None else out.split("\0")[0]


def get_all(key: str, *, scope=None, file=None, cwd=None, env=None) -> list[str]:
    out = _read([*_where(scope, file), "-z", "--get-all", key], cwd, env)
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


def get_all_with_origin(
    key: str, *, scope=None, file=None, includes: bool = False, cwd=None, env=None
) -> list[Origin]:
    """Every value of `key`, in the order git reads them, with the file each comes from.
    includes: also read the files that [include] and [includeIf] pull in; git checks
    [includeIf] conditions against the repository at cwd."""
    flags = ["--includes"] if includes else []
    args = [*_where(scope, file), *flags, "--show-origin", "-z", "--get-all", key]
    out = _read(args, cwd, env)
    if out is None:
        return []
    fields = out.split("\0")[:-1]
    origins = []
    for origin, value in zip(fields[::2], fields[1::2], strict=True):  # origin, value pairs
        kind, _, origin_path = origin.partition(":")
        origins.append(Origin(kind=kind, path=origin_path, value=value))
    return origins


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
    key: str,
    value: str,
    *,
    scope=None,
    file=None,
    cwd=None,
    replace_all: bool = False,
    value_regex: str | None = None,
) -> None:
    """Set one value. git refuses (exit 5) when the key has several, unless replace_all.
    value_regex (POSIX extended): only replace the values it matches; none matching adds one."""
    if file is not None:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
    flags = ["--replace-all"] if replace_all else []
    pattern = [value_regex] if value_regex is not None else []
    run_git("config", *_where(scope, file), *flags, key, value, *pattern, cwd=cwd)


def add_value(key: str, value: str, *, scope=None, file=None, cwd=None) -> None:
    if file is not None:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
    run_git("config", *_where(scope, file), "--add", key, value, cwd=cwd)


def unset(key: str, *, scope=None, file=None, cwd=None, value_regex: str | None = None) -> None:
    """Remove every value of key, or only those value_regex (POSIX extended) matches."""
    if file is not None and not Path(file).exists():
        return
    pattern = [value_regex] if value_regex is not None else []
    proc = run_git(
        "config", *_where(scope, file), "--unset-all", key, *pattern, cwd=cwd, check=False
    )
    if proc.returncode not in (0, 5):  # 5 = key was not set, or no value matched
        raise GitShiftError(f"could not unset {key}: {proc.stderr.strip()}")


def git_version() -> tuple[int, int]:
    out = run_git("--version").stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    if match is None:
        raise GitShiftError(f"cannot parse git version from {out!r}")
    return int(match.group(1)), int(match.group(2))
