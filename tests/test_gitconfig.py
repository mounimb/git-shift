import os
from pathlib import Path

import pytest
from helpers import git

from git_shift import gitconfig
from git_shift.errors import GitShiftError


def test_set_and_get_in_file(home, tmp_path):
    path = tmp_path / "x.gitconfig"
    gitconfig.set_value("user.email", "a@example.com", file=path)
    assert gitconfig.get("user.email", file=path) == "a@example.com"


def test_get_missing_returns_none(home, tmp_path):
    assert gitconfig.get("user.email", file=tmp_path / "missing.gitconfig") is None


def test_global_scope_writes_to_isolated_home(home):
    gitconfig.set_value("user.name", "Test User", scope="global")
    assert "Test User" in (home / ".gitconfig").read_text(encoding="utf-8")


def test_add_and_get_all(home):
    gitconfig.add_value("include.path", "/a", scope="global")
    gitconfig.add_value("include.path", "/b", scope="global")
    assert gitconfig.get_all("include.path", scope="global") == ["/a", "/b"]


def test_list_entries_keeps_order_and_spaces(home, tmp_path):
    path = tmp_path / "c.gitconfig"
    gitconfig.set_value("user.name", "Jane Doe", file=path)
    gitconfig.set_value("includeIf.gitdir/i:/a b/.path", "/x y", file=path)
    assert gitconfig.list_entries(file=path) == [
        ("user.name", "Jane Doe"),
        ("includeif.gitdir/i:/a b/.path", "/x y"),
    ]


def test_get_with_origin_reports_the_file(home, make_repo, tmp_path):
    repo = make_repo(tmp_path / "repo")
    git(repo, "config", "user.email", "local@example.com")
    origin = gitconfig.get_with_origin("user.email", cwd=repo)
    assert origin.kind == "file"
    assert origin.value == "local@example.com"
    assert origin.path.replace("\\", "/").endswith(".git/config")


def test_get_with_origin_from_a_subfolder_is_absolute(home, make_repo, tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    git(repo, "config", "user.email", "local@example.com")
    origin = gitconfig.get_with_origin("user.email", cwd=repo / "sub")
    assert Path(origin.path).resolve() == (repo / ".git" / "config").resolve()


def test_unset_missing_key_is_not_an_error(home, tmp_path):
    gitconfig.unset("user.email", file=tmp_path / "c.gitconfig")
    gitconfig.unset("user.email", scope="global")


def test_git_version_is_recent_enough():
    assert gitconfig.git_version() >= (2, 13)


def test_run_git_never_uses_a_git_from_the_current_directory(home, monkeypatch):
    planted = home / ("git.exe" if os.name == "nt" else "git")
    planted.write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(home)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([".", os.environ["PATH"]]))
    assert gitconfig.run_git("--version").stdout.startswith("git version")


def test_home_fixture_hides_every_real_config(home):
    proc = gitconfig.run_git("config", "--show-origin", "--list", check=False)
    assert proc.stdout == ""


def test_broken_config_file_raises(home, tmp_path):
    path = tmp_path / "bad.gitconfig"
    path.write_text("[user\n", encoding="utf-8")
    with pytest.raises(GitShiftError):
        gitconfig.get("user.email", file=path)
    with pytest.raises(GitShiftError):
        gitconfig.list_entries(file=path)


def test_list_entries_of_missing_config_is_empty(home, tmp_path):
    assert gitconfig.list_entries(file=tmp_path / "missing.gitconfig") == []
    assert gitconfig.list_entries(scope="global") == []
