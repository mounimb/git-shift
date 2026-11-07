import os
import shlex
import sys

import pytest
from helpers import git

from git_shift import gitconfig, guard, paths, store
from git_shift.cli import main
from git_shift.errors import GitShiftError


def write_script(directory, name, body):
    """An sh script git can run: LF endings, and the exec bit that git requires on POSIX."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


def commit(repo, message="test"):
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.com")
    git(repo, *identity, "commit", "--allow-empty", "-q", "-m", message)


def test_install_sets_global_hooks_path_and_writes_lf_scripts(home):
    guard.install()
    assert guard.is_installed()
    hooks = paths.hooks_dir()
    for name in ("pre-commit", "pre-push", "commit-msg", "post-checkout"):
        text = (hooks / name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert "\r" not in text
        assert os.access(hooks / name, os.X_OK)
    assert "__hook pre-commit" in (hooks / "pre-commit").read_text(encoding="utf-8")
    assert "__hook" not in (hooks / "commit-msg").read_text(encoding="utf-8")
    assert (hooks / guard.LAUNCHER).is_file()


def test_previous_global_hooks_path_is_chained_then_restored(home, tmp_path):
    previous = tmp_path / "myhooks"
    previous.mkdir()
    git(home, "config", "--global", "core.hooksPath", str(previous))
    assert guard.install() == str(previous)
    script = (paths.hooks_dir() / "pre-commit").read_text(encoding="utf-8")
    assert paths.to_git_path(previous) in script
    guard.install()  # reinstalling must not lose the original value
    guard.uninstall()
    assert gitconfig.get("core.hooksPath", scope="global") == str(previous)
    assert not paths.hooks_dir().exists()


def test_uninstall_unsets_when_there_was_no_previous(home):
    guard.install()
    guard.uninstall()
    assert gitconfig.get("core.hooksPath", scope="global") is None


def test_uninstall_without_install_fails(home):
    with pytest.raises(GitShiftError, match="not installed"):
        guard.uninstall()


def test_cli_guard_install_and_uninstall(home, capsys):
    assert main(["guard", "install"]) == 0
    assert "guard installed" in capsys.readouterr().out
    assert main(["guard", "uninstall"]) == 0
    assert "guard removed" in capsys.readouterr().out


def test_install_and_reinstall_leave_no_temp_files(home):
    guard.install()
    guard.install()
    expected = sorted([*guard.GUARDED, *guard.PASSTHROUGH, guard.LAUNCHER])
    assert sorted(path.name for path in paths.hooks_dir().iterdir()) == expected
    assert [p for p in paths.config_dir().rglob("*") if p.name.endswith(".tmp")] == []


def test_a_failed_install_leaves_no_temp_file_and_git_untouched(home, monkeypatch):
    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store, "replace_file", fail)
    with pytest.raises(OSError, match="disk full"):
        guard.install()
    assert list(paths.hooks_dir().iterdir()) == []
    assert gitconfig.get("core.hooksPath", scope="global") is None


def test_several_global_hooks_paths_save_the_last_and_restore_only_it(home, tmp_path):
    first, last = str(tmp_path / "first"), str(tmp_path / "last")
    git(home, "config", "--global", "--add", "core.hooksPath", first)
    git(home, "config", "--global", "--add", "core.hooksPath", last)
    assert guard.install() == last  # git uses the last value
    ours = paths.to_git_path(paths.hooks_dir())
    assert gitconfig.get_all("core.hooksPath", scope="global") == [ours]
    assert paths.to_git_path(last) in (paths.hooks_dir() / "pre-commit").read_text("utf-8")
    assert guard.uninstall() == last
    assert gitconfig.get_all("core.hooksPath", scope="global") == [last]


def test_a_previous_value_in_the_xdg_file_is_chained_and_left_alone(home, make_repo, tmp_path):
    previous = tmp_path / "xdg-hooks"
    write_script(previous, "commit-msg", "echo xdg >> ran.txt")
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True)
    git(home, "config", "--file", str(xdg), "core.hooksPath", str(previous))
    xdg_before = xdg.read_bytes()
    # with both files present, --global reads and writes only ~/.gitconfig, which git reads
    # last, but git still uses the XDG value until then
    (home / ".gitconfig").write_text("[user]\n\tname = Test\n", encoding="utf-8")
    assert guard.install() == str(previous)
    repo = make_repo(home / "repo")
    commit(repo)
    assert (repo / "ran.txt").read_text(encoding="utf-8") == "xdg\n"
    guard.uninstall()
    assert xdg.read_bytes() == xdg_before
    assert gitconfig.get_all("core.hooksPath", file=home / ".gitconfig") == []


@pytest.mark.parametrize("saved_too", [False, True])
def test_our_own_hooks_dir_is_never_chained(home, saved_too):
    ours = paths.to_git_path(paths.hooks_dir())
    git(home, "config", "--global", "core.hooksPath", ours)  # copied, or the state was lost
    if saved_too:
        gitconfig.set_value(guard.STATE_PREVIOUS, ours, file=paths.state_file())
    assert guard.install() is None
    script = (paths.hooks_dir() / "pre-commit").read_text(encoding="utf-8")
    assert 'GIT_SHIFT_CHAIN="$(git rev-parse --git-common-dir)/hooks"' in script
    assert gitconfig.get(guard.STATE_PREVIOUS, file=paths.state_file()) is None
    assert guard.uninstall() is None
    assert gitconfig.get("core.hooksPath", scope="global") is None


def test_uninstall_refuses_while_the_xdg_file_still_points_to_the_guard(home):
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True)
    xdg.write_text("", encoding="utf-8")  # the only global file: --global writes here
    guard.install()
    (home / ".gitconfig").write_text("[user]\n\tname = Test\n", encoding="utf-8")  # later
    with pytest.raises(GitShiftError, match="also points to git-shift"):
        guard.uninstall()
    assert guard.is_installed()  # git still uses the XDG value: the hooks must stay
    assert (paths.hooks_dir() / "pre-commit").is_file()


def test_is_installed_ignores_case_only_where_the_filesystem_does(home):
    guard.install()
    ours = paths.to_git_path(paths.hooks_dir())
    git(home, "config", "--global", "core.hooksPath", ours.upper())
    assert guard.is_installed() == (os.name == "nt")


def test_installed_python_reads_back_a_path_with_spaces_and_quotes(home, monkeypatch):
    python = '/opt/my tools/it\'s "$HOME"/bin/python3'
    monkeypatch.setattr(sys, "executable", python)
    guard.install()
    assert guard.installed_python() == python


def test_hooks_run_a_python_and_launcher_whose_paths_need_quoting(
    home, make_repo, monkeypatch, tmp_path
):
    odd = tmp_path / "it's a $dir"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(odd / "config"))  # moves the launcher too
    seen = tmp_path / "python-args.txt"
    fake_python = write_script(
        odd, "py thon", f"printf '%s\\n' \"$@\" > {shlex.quote(paths.to_git_path(seen))}"
    )
    monkeypatch.setattr(sys, "executable", str(fake_python))
    guard.install()
    commit(make_repo(home / "repo"))
    launcher = paths.to_git_path(paths.hooks_dir() / guard.LAUNCHER)
    assert seen.read_text(encoding="utf-8").splitlines() == [
        "-I",
        launcher,
        "__hook",
        "pre-commit",
    ]


def test_a_passthrough_hook_runs_the_repository_hook_with_its_arguments(home, make_repo):
    repo = make_repo(home / "repo")
    write_script(repo / ".git" / "hooks", "commit-msg", 'cat "$1" > seen.txt')
    guard.install()
    commit(repo, "hello")
    assert (repo / "seen.txt").read_text(encoding="utf-8").startswith("hello")


def test_hooks_of_a_previous_global_dir_with_a_space_run_instead(home, make_repo, tmp_path):
    repo = make_repo(home / "repo")
    previous = tmp_path / "my hooks"
    for directory, label in ((previous, "previous"), (repo / ".git" / "hooks", "local")):
        write_script(directory, "pre-commit", f"echo {label}-pre-commit >> ran.txt")
        write_script(directory, "commit-msg", f"echo {label}-commit-msg >> ran.txt")
    git(home, "config", "--global", "core.hooksPath", str(previous))
    guard.install()
    commit(repo)
    lines = (repo / "ran.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["previous-pre-commit", "previous-commit-msg"]


def test_pre_push_hands_its_input_to_the_repository_hook(home, make_repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = make_repo(home / "repo", remote=str(remote))
    commit(repo)
    write_script(repo / ".git" / "hooks", "pre-push", 'cat > pushed.txt\necho "$1" >> pushed.txt')
    guard.install()
    git(repo, "push", "-q", "origin", "main")
    sha = git(repo, "rev-parse", "HEAD")
    zeros = "0" * len(sha)
    assert (repo / "pushed.txt").read_text(encoding="utf-8") == (
        f"refs/heads/main {sha} refs/heads/main {zeros}\norigin\n"
    )
