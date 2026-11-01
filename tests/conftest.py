from __future__ import annotations

import pytest
from helpers import git

from git_shift import gh  # placeholder until Task 11, see the note below

_GIT_ENV = (
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "EMAIL",
    "GIT_SHIFT_SKIP",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME: tests never read or write the developer's real git config."""
    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in _GIT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gh, "_executable", lambda: None)  # tests opt in to a fake gh
    return home


@pytest.fixture
def make_repo(home):
    def _make(path, remote=None):
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q", "-b", "main")
        if remote:
            git(path, "remote", "add", "origin", remote)
        return path

    return _make
