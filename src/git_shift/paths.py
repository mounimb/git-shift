"""Filesystem locations and path normalisation shared by git-shift modules."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Home directory as git resolves it: HOME first, then USERPROFILE (Windows)."""
    value = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return Path(value) if value else Path.home()


def _xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else home() / ".config"


def config_dir() -> Path:
    return _xdg_config_home() / "git-shift"


def identities_dir() -> Path:
    return config_dir() / "identities"


def hooks_dir() -> Path:
    return config_dir() / "hooks"


def backups_dir() -> Path:
    return config_dir() / "backups"


def rules_file() -> Path:
    return config_dir() / "rules.gitconfig"


def state_file() -> Path:
    return config_dir() / "state.gitconfig"


def xdg_git_config() -> Path:
    """git's alternative global config file."""
    return _xdg_config_home() / "git" / "config"


def expand(path: str) -> str:
    """Expand a leading ~ with the same home directory git uses."""
    if path == "~" or path.startswith(("~/", "~\\")):
        return str(home()) + path[1:]
    return path


def to_git_path(path: str | os.PathLike[str]) -> str:
    """Absolute, symlink-resolved path with forward slashes (what git config expects)."""
    resolved = os.path.realpath(expand(os.fspath(path)))
    return resolved.replace("\\", "/")


def to_folder_rule(path: str | os.PathLike[str]) -> str:
    """Folder path for an includeIf rule: normalised, always ending with one '/'."""
    git_path = to_git_path(path)
    return git_path if git_path.endswith("/") else git_path + "/"


def is_within(path: str | os.PathLike[str], folder_rule: str) -> bool:
    """True if path is the folder itself or inside it. Case-insensitive, like gitdir/i."""
    return to_folder_rule(path).casefold().startswith(folder_rule.casefold())


def depth(folder_rule: str) -> int:
    return len([part for part in folder_rule.split("/") if part])
