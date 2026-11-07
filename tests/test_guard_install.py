import getpass
import os
import shlex
import subprocess
import sys

import pytest
from helpers import git

from git_shift import gitconfig, guard, paths, store
from git_shift.cli import main
from git_shift.errors import GitShiftError

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.com")
LOOP_WARNING = "git-shift: warning: core.hooksPath loops back to git-shift, hooks skipped"


def write_script(directory, name, body):
    """An sh script git can run: LF endings, and the exec bit that git requires on POSIX."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


def run(cwd, *args):
    """git that may fail. The timeout stops a test if hooks loop forever."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60
    )


def commit(repo, message="test"):
    git(repo, *IDENTITY, "commit", "--allow-empty", "-q", "-m", message)


def try_commit(repo):
    return run(repo, *IDENTITY, "commit", "--allow-empty", "-q", "-m", "test")


def with_remote(make_repo, home, tmp_path):
    """A repository with one commit and an empty bare remote, as (repo, remote)."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = make_repo(home / "repo", remote=str(remote))
    commit(repo)
    return repo, remote


def push_line(repo):
    """The line git gives pre-push for a first push of main."""
    sha = git(repo, "rev-parse", "HEAD")
    return f"refs/heads/main {sha} refs/heads/main {'0' * len(sha)}\n"


@pytest.fixture
def fake_python(home, tmp_path, monkeypatch):
    """Hooks installed after make(body) run this sh script instead of Python, with the same
    arguments, from the repository's top level."""

    def make(body):
        script = write_script(tmp_path / "fake python", "python", body)
        monkeypatch.setattr(sys, "executable", str(script))
        return script

    return make


def effective_hooks_path(cwd):
    """core.hooksPath as git reads it outside a repository: "" when unset."""
    return run(cwd, "config", "--get", "core.hooksPath").stdout.strip()


# install and uninstall


def test_install_sets_global_hooks_path_and_writes_lf_scripts(home):
    guard.install()
    assert guard.is_installed()
    hooks = paths.hooks_dir()
    for name in (*guard.GUARDED, *guard.PASSTHROUGH):
        text = (hooks / name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert "\r" not in text
        assert os.access(hooks / name, os.X_OK)
        assert text.splitlines()[2].startswith("case $GIT_SHIFT_DEPTH in")  # first command
    assert "__hook pre-commit" in (hooks / "pre-commit").read_text(encoding="utf-8")
    assert "__hook" not in (hooks / "commit-msg").read_text(encoding="utf-8")
    assert (hooks / guard.LAUNCHER).is_file()
    for left_out in ("push-to-checkout", "proc-receive", "update", "post-update"):
        assert not (hooks / left_out).exists()


def test_previous_global_hooks_path_is_chained_then_restored(home, tmp_path):
    previous = tmp_path / "myhooks"
    previous.mkdir()
    git(home, "config", "--global", "core.hooksPath", str(previous))
    assert guard.install().chain == str(previous)
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
    out = capsys.readouterr().out
    assert "guard installed" in out
    assert "chained to each repository's own hooks" in out
    assert "not active" not in out
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
    assert guard.install().chain == last  # git uses the last value
    ours = paths.to_git_path(paths.hooks_dir())
    assert gitconfig.get_all("core.hooksPath", scope="global") == [ours]
    assert paths.to_git_path(last) in (paths.hooks_dir() / "pre-commit").read_text("utf-8")
    assert guard.uninstall() == last
    assert gitconfig.get_all("core.hooksPath", scope="global") == [last]


def test_uninstall_restores_the_previous_value_in_one_write(home, tmp_path, monkeypatch):
    previous = str(tmp_path / "mine")
    git(home, "config", "--global", "core.hooksPath", previous)
    guard.install()
    ours = paths.to_git_path(paths.hooks_dir())
    seen = []
    real_run_git = gitconfig.run_git

    def spy(*args, **kwargs):
        proc = real_run_git(*args, **kwargs)
        if not {"--get", "--get-all", "--list"} & set(args):  # a write
            seen.append(effective_hooks_path(home))
        return proc

    monkeypatch.setattr(gitconfig, "run_git", spy)
    guard.uninstall()
    assert seen and all(value in (ours, previous) for value in seen)  # never unset
    assert seen[-1] == previous


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
    assert guard.install().chain == str(previous)
    repo = make_repo(home / "repo")
    commit(repo)
    assert (repo / "ran.txt").read_text(encoding="utf-8") == "xdg\n"
    guard.uninstall()
    assert xdg.read_bytes() == xdg_before
    assert gitconfig.get_all("core.hooksPath", file=home / ".gitconfig") == []


def test_uninstall_also_removes_the_guard_from_the_xdg_file(home):
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True)
    xdg.write_text("[user]\n\tname = Test\n", encoding="utf-8")  # the only one: --global uses it
    guard.install()
    (home / ".gitconfig").write_text("[user]\n\tname = Test\n", encoding="utf-8")  # later
    assert guard.is_installed()  # git still reads the XDG value
    guard.uninstall()
    assert gitconfig.get_all("core.hooksPath", file=xdg) == []
    assert gitconfig.get("user.name", file=xdg) == "Test"
    assert effective_hooks_path(home) == ""
    assert not paths.hooks_dir().exists()


def test_with_no_global_value_the_system_hooks_are_chained_but_not_saved(
    home, make_repo, monkeypatch, tmp_path, capsys
):
    system_hooks = tmp_path / "system hooks"
    write_script(system_hooks, "commit-msg", "echo system >> ran.txt")
    extra = tmp_path / "system-extra.gitconfig"  # read through an [include]
    extra.write_text(f"[core]\n\thooksPath = {paths.to_git_path(system_hooks)}\n", "utf-8")
    system = tmp_path / "system.gitconfig"
    system.write_text(f"[include]\n\tpath = {paths.to_git_path(extra)}\n", encoding="utf-8")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    assert main(["guard", "install"]) == 0
    assert f"chained to the system hooks: {paths.to_git_path(system_hooks)}" in (
        capsys.readouterr().out
    )
    assert gitconfig.get(guard.STATE_PREVIOUS, file=paths.state_file()) is None
    repo = make_repo(home / "repo")
    commit(repo)
    assert (repo / "ran.txt").read_text(encoding="utf-8") == "system\n"
    assert guard.uninstall() is None
    assert gitconfig.get_all("core.hooksPath", file=home / ".gitconfig") == []
    assert effective_hooks_path(home) == paths.to_git_path(system_hooks)  # back by itself


def test_install_warns_when_an_include_overrides_the_guard(home, tmp_path, capsys):
    other = paths.to_git_path(tmp_path / "other")
    (home / "extra.gitconfig").write_text(f"[core]\n\thooksPath = {other}\n", encoding="utf-8")
    # git adds core.hooksPath to the existing [core] section, before the include
    (home / ".gitconfig").write_text(
        "[core]\n\tautocrlf = false\n[include]\n\tpath = extra.gitconfig\n", encoding="utf-8"
    )
    assert main(["guard", "install"]) == 0
    out = capsys.readouterr().out
    assert f"the guard is not active: git uses core.hooksPath {other}" in out
    assert "extra.gitconfig" in out
    assert not guard.is_installed()
    assert main(["guard", "uninstall"]) == 0  # still possible
    assert gitconfig.get_all("core.hooksPath", file=home / ".gitconfig") == []
    assert effective_hooks_path(home) == other


def test_a_previous_hooks_path_under_another_users_home_is_expanded(home):
    user = getpass.getuser()
    git(home, "config", "--global", "core.hooksPath", f"~{user}/hooks")
    guard.install()
    expected = paths.to_git_path(os.path.expanduser(f"~{user}/hooks"))
    assert "~" not in expected
    script = (paths.hooks_dir() / "pre-commit").read_text(encoding="utf-8")
    assert f"GIT_SHIFT_CHAIN={shlex.quote(expected)}\n" in script


# loops


@pytest.mark.parametrize("saved_too", [False, True])
def test_our_own_hooks_dir_is_never_chained(home, saved_too):
    ours = paths.to_git_path(paths.hooks_dir())
    git(home, "config", "--global", "core.hooksPath", ours)  # copied, or the state was lost
    if saved_too:
        gitconfig.set_value(guard.STATE_PREVIOUS, ours, file=paths.state_file())
    assert guard.install().chain is None
    script = (paths.hooks_dir() / "pre-commit").read_text(encoding="utf-8")
    assert 'GIT_SHIFT_CHAIN="$(git rev-parse --git-common-dir)/hooks"' in script
    assert gitconfig.get(guard.STATE_PREVIOUS, file=paths.state_file()) is None
    assert guard.uninstall() is None
    assert gitconfig.get("core.hooksPath", scope="global") is None


def test_another_guard_dir_is_never_chained(home, monkeypatch, tmp_path):
    chains = []
    for config in ("x", "y", "x"):  # y would chain to x, then x to y
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / config))
        chains.append(guard.install().chain)
    assert chains == [None, None, None]


def test_a_hooks_path_loop_stops_at_runtime(home, make_repo, monkeypatch, tmp_path, fake_python):
    fake_python("exit 0")
    # install refuses this loop, but hand edits or older versions can still make one
    monkeypatch.setattr(guard, "_chainable", lambda value: True)
    for config in ("x", "y", "x"):  # y chains to x, then x to y
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / config))
        guard.install()
    proc = try_commit(make_repo(home / "repo"))
    assert proc.returncode == 0, proc.stderr
    assert LOOP_WARNING in proc.stderr


def test_a_depth_that_is_not_a_number_is_reset(home, make_repo, monkeypatch):
    repo = make_repo(home / "repo")
    write_script(repo / ".git" / "hooks", "commit-msg", "echo ran > ran.txt")
    guard.install()
    monkeypatch.setenv("GIT_SHIFT_DEPTH", "x; exit 7")
    proc = try_commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert (repo / "ran.txt").exists()


# paths that need quoting


def test_is_installed_ignores_case_only_where_the_filesystem_does(home):
    guard.install()
    other_case = paths.to_git_path(paths.hooks_dir()).upper()
    git(home, "config", "--global", "core.hooksPath", other_case)
    assert guard.is_installed() == os.path.exists(other_case)


def test_installed_python_reads_back_a_path_with_spaces_and_quotes(home, monkeypatch):
    python = '/opt/my tools/it\'s "$HOME"/bin/python3'
    monkeypatch.setattr(sys, "executable", python)
    guard.install()
    assert guard.installed_python() == python


def test_hooks_run_a_python_and_launcher_whose_paths_need_quoting(
    home, make_repo, monkeypatch, tmp_path
):
    odd = tmp_path / "it's [a] $dir (1)"
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
    guard.uninstall()  # matches the odd value exactly when removing it
    assert effective_hooks_path(home) == ""


def test_a_missing_python_skips_the_guard_and_says_how_to_fix_it(home, make_repo, fake_python):
    fake_python("exit 3").unlink()  # it would block
    guard.install()
    proc = try_commit(make_repo(home / "repo"))
    assert proc.returncode == 0, proc.stderr
    assert "missing, guard skipped (fix: git shift guard install)" in proc.stderr


def test_the_launcher_fails_open_once_git_shift_is_gone(home, tmp_path):
    gone = tmp_path / "gone"
    (gone / "git_shift").mkdir(parents=True)  # found first, and has no cli module
    (gone / "git_shift" / "__init__.py").write_text("", encoding="utf-8")
    launcher = tmp_path / "launch.py"
    launcher.write_text(guard.render_launcher(str(gone)), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", str(launcher), "__hook", "pre-commit"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stderr == (
        "git-shift: warning: git-shift is no longer installed for this Python; "
        "remove the guard with: git config --global --unset core.hooksPath\n"
    )


# chaining, in real commits and pushes


def test_a_passthrough_hook_runs_the_repository_hook_with_its_arguments(home, make_repo):
    repo = make_repo(home / "repo")
    write_script(repo / ".git" / "hooks", "commit-msg", 'cat "$1" > seen.txt')
    guard.install()
    commit(repo, "hello")
    assert (repo / "seen.txt").read_text(encoding="utf-8").startswith("hello")


def test_a_linked_worktree_runs_its_repository_hooks(home, make_repo):
    repo = make_repo(home / "repo")
    commit(repo)
    write_script(repo / ".git" / "hooks", "commit-msg", 'cat "$1" > seen.txt')
    worktree = home / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "other", str(worktree))
    guard.install()
    commit(worktree, "from the worktree")  # its .git is a file
    assert (worktree / "seen.txt").read_text(encoding="utf-8").startswith("from the worktree")


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


@pytest.mark.parametrize("hook", ["pre-commit", "commit-msg"])
def test_a_failing_repository_hook_still_fails_the_commit(home, make_repo, fake_python, hook):
    fake_python("exit 0")
    repo = make_repo(home / "repo")
    write_script(repo / ".git" / "hooks", hook, "exit 1")
    guard.install()
    assert try_commit(repo).returncode != 0


def test_a_failing_repository_pre_push_still_fails_the_push(home, make_repo, tmp_path, fake_python):
    fake_python("exit 0")
    repo, remote = with_remote(make_repo, home, tmp_path)
    write_script(repo / ".git" / "hooks", "pre-push", "exit 1")
    guard.install()
    assert run(repo, "push", "-q", "origin", "main").returncode != 0
    assert run(remote, "rev-parse", "--verify", "-q", "refs/heads/main").returncode != 0


def test_a_blocking_check_stops_the_commit_before_the_repository_hook(home, make_repo, fake_python):
    fake_python(f"exit {guard.BLOCK_EXIT}")
    repo = make_repo(home / "repo")
    write_script(repo / ".git" / "hooks", "pre-commit", "echo ran > ran.txt")
    guard.install()
    assert try_commit(repo).returncode != 0
    assert not (repo / "ran.txt").exists()


def test_a_crashing_check_lets_the_commit_through_and_still_chains(home, make_repo, fake_python):
    fake_python("exit 1")  # Python's exit code for an uncaught exception
    repo = make_repo(home / "repo")
    write_script(repo / ".git" / "hooks", "pre-commit", "echo ran > ran.txt")
    guard.install()
    proc = try_commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert (repo / "ran.txt").exists()


def test_pre_push_input_reaches_both_the_check_and_the_repository_hook(
    home, make_repo, tmp_path, fake_python
):
    fake_python("cat > python-stdin.txt")
    repo, _ = with_remote(make_repo, home, tmp_path)
    write_script(repo / ".git" / "hooks", "pre-push", "cat > hook-stdin.txt")
    guard.install()
    git(repo, "push", "-q", "origin", "main")
    expected = push_line(repo)
    assert (repo / "python-stdin.txt").read_text(encoding="utf-8") == expected
    assert (repo / "hook-stdin.txt").read_text(encoding="utf-8") == expected


def test_pre_push_hands_its_input_and_arguments_to_the_repository_hook(home, make_repo, tmp_path):
    repo, _ = with_remote(make_repo, home, tmp_path)
    write_script(repo / ".git" / "hooks", "pre-push", 'cat > pushed.txt\necho "$1" >> pushed.txt')
    guard.install()
    git(repo, "push", "-q", "origin", "main")
    assert (repo / "pushed.txt").read_text(encoding="utf-8") == push_line(repo) + "origin\n"


def test_receive_hooks_run_the_remote_repository_hooks(home, make_repo, tmp_path):
    repo, remote = with_remote(make_repo, home, tmp_path)
    for name in ("pre-receive", "post-receive"):
        write_script(remote / "hooks", name, f"cat > {name}.txt")
    guard.install()
    git(repo, "push", "-q", "origin", "main")
    sha = git(repo, "rev-parse", "HEAD")
    for name in ("pre-receive", "post-receive"):
        assert (remote / f"{name}.txt").read_text("utf-8") == (
            f"{'0' * len(sha)} {sha} refs/heads/main\n"
        )


def test_a_failing_remote_pre_receive_still_rejects_the_push(
    home, make_repo, tmp_path, fake_python
):
    fake_python("exit 0")
    repo, remote = with_remote(make_repo, home, tmp_path)
    write_script(remote / "hooks", "pre-receive", "exit 1")
    guard.install()
    assert run(repo, "push", "-q", "origin", "main").returncode != 0
    assert run(remote, "rev-parse", "--verify", "-q", "refs/heads/main").returncode != 0


def test_a_push_to_a_checked_out_branch_still_updates_its_worktree(home, make_repo):
    target = make_repo(home / "target")
    commit(target)
    git(target, "config", "receive.denyCurrentBranch", "updateInstead")
    source = home / "source"
    git(home, "clone", "-q", str(target), str(source))
    (source / "pushed.txt").write_text("hello\n", encoding="utf-8")
    git(source, "add", "pushed.txt")
    commit(source)
    guard.install()
    git(source, "push", "-q", "origin", "main")
    assert (target / "pushed.txt").read_text(encoding="utf-8") == "hello\n"
