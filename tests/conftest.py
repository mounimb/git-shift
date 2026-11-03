from __future__ import annotations

import os

import pytest
from helpers import git

from git_shift import gh


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME: tests never read or write the developer's real git config."""
    home = tmp_path.resolve() / "home"
    home.mkdir()
    for name in list(os.environ):
        if name.startswith("GIT_") or name == "EMAIL":  # also clears GIT_SHIFT_SKIP
            monkeypatch.delenv(name)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):  # status reports them: tests opt in
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    monkeypatch.chdir(home)  # a call without cwd can never touch the git-shift checkout
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
