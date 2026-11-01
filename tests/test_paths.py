import os
from pathlib import Path

import pytest

from git_shift import paths

windows_only = pytest.mark.skipif(os.name != "nt", reason="Git for Windows home lookup")


def test_home_prefers_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.home() == tmp_path


@windows_only
def test_home_without_home_env_uses_homedrive_homepath(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    drive, rest = os.path.splitdrive(str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", rest)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert paths.home() == tmp_path


@windows_only
def test_home_falls_back_to_userprofile_when_homepath_is_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    drive, rest = os.path.splitdrive(str(tmp_path / "missing"))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", rest)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert paths.home() == tmp_path / "profile"


def test_config_dir_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.config_dir() == tmp_path / "cfg" / "git-shift"


def test_config_dir_defaults_to_home_dot_config(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / ".config" / "git-shift"


def test_to_git_path_is_absolute_with_forward_slashes(tmp_path):
    result = paths.to_git_path(tmp_path / "a" / "b")
    assert "\\" not in result
    assert os.path.isabs(result)
    assert result.endswith("/a/b")


def test_to_git_path_expands_tilde(monkeypatch, tmp_path):
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    assert paths.to_git_path("~/code") == paths.to_git_path(home / "code")


def test_to_folder_rule_adds_one_trailing_slash(tmp_path):
    rule = paths.to_folder_rule(tmp_path / "code")
    assert rule.endswith("/code/")
    assert paths.to_folder_rule(rule) == rule


def test_gitdir_pattern_escapes_wildmatch_characters():
    assert paths.gitdir_pattern("/x/code [old]/") == "/x/code \\[old]/"
    assert paths.gitdir_pattern("/x/a*b?/") == "/x/a\\*b\\?/"
    assert paths.gitdir_pattern("C:/Users/me/code/") == "C:/Users/me/code/"


def test_is_within_matches_folder_and_subfolders_case_insensitively(tmp_path):
    rule = paths.to_folder_rule(tmp_path / "Code")
    assert paths.is_within(tmp_path / "Code", rule)
    assert paths.is_within(tmp_path / "code" / "app", rule)
    assert not paths.is_within(tmp_path / "Codebase", rule)


def test_is_within_rejects_malformed_rules(tmp_path):
    assert not paths.is_within(tmp_path / "code", "")
    bare = paths.to_folder_rule(tmp_path / "Code").rstrip("/")
    assert not paths.is_within(tmp_path / "Codebase", bare)


def test_to_folder_rule_resolves_symlinks(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not permitted here")
    rule = paths.to_folder_rule(target)
    assert paths.to_folder_rule(link) == rule
    assert paths.is_within(link / "app", rule)


def test_depth_counts_segments():
    assert paths.depth("C:/Users/me/code/") == 4
    assert paths.depth("/home/me/code/") == 3


def test_find_executable_skips_relative_path_entries(monkeypatch, tmp_path):
    name = "git.exe" if os.name == "nt" else "git"
    planted = tmp_path / name
    planted.write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", os.environ["PATH"]]))
    found = paths.find_executable("git")
    assert found is not None and os.path.isabs(found)
    assert Path(found).resolve().parent != tmp_path.resolve()


def test_find_executable_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert paths.find_executable("git") is None
