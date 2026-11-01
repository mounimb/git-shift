import os

from git_shift import paths


def test_home_prefers_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.home() == tmp_path


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


def test_is_within_matches_folder_and_subfolders_case_insensitively(tmp_path):
    rule = paths.to_folder_rule(tmp_path / "Code")
    assert paths.is_within(tmp_path / "Code", rule)
    assert paths.is_within(tmp_path / "code" / "app", rule)
    assert not paths.is_within(tmp_path / "Codebase", rule)


def test_depth_counts_segments():
    assert paths.depth("C:/Users/me/code/") == 4
    assert paths.depth("/home/me/code/") == 3
