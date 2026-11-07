"""The pre-commit check in real commits: git runs the guard hooks, which run this Python
and the real check."""

import io

import pytest
from helpers import git, run, write_script

from git_shift import guard, store
from git_shift.cli import main
from git_shift.errors import GitShiftError

EMAIL = "me@example.com"
WRONG = "wrong@example.com"
OTHER = "Other <other@example.com>"
SKIPPED = "git-shift: warning: guard skipped after an error:"


def commit(repo, *extra):
    """A real commit, which the guard may block."""
    return run(repo, "commit", "--allow-empty", "-q", "-m", "test", *extra)


def head(repo, field="%ae"):
    """A field of HEAD (default: the author email), or None before the first commit."""
    proc = run(repo, "log", "-1", f"--format={field}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def add(home, name, email, folder, gh=None):
    identity, _ = store.add_identity(
        name, email=email, user_name=name.title(), folder=str(home / folder), gh=gh
    )
    return identity


@pytest.fixture
def repo(home, make_repo):
    """A repository in the folder of the identity "perso", with the guard installed."""
    add(home, "perso", EMAIL, "code")
    guard.install()
    return make_repo(home / "code" / "app")


def outside_every_folder(home, make_repo, owner):
    """A guarded repository outside every folder, whose origin belongs to owner on GitHub.
    The identity "perso" has the GitHub login jane."""
    add(home, "perso", EMAIL, "code", gh="jane")
    guard.install()
    git(home, "config", "--global", "user.email", "other@example.com")
    git(home, "config", "--global", "user.name", "Other")
    return make_repo(home / "elsewhere" / "x", remote=f"https://github.com/{owner}/x.git")


# what the check decides


def test_the_right_identity_passes(repo):
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert head(repo) == EMAIL


def test_a_local_user_email_is_blocked_and_the_fix_works(repo, monkeypatch):
    git(repo, "config", "user.email", WRONG)
    proc = commit(repo)
    assert proc.returncode != 0
    assert head(repo) is None
    assert f'this repo belongs to "perso", but you are committing as {WRONG}' in proc.stderr
    assert f"expected: {EMAIL}  (matched by folder {store.load('perso').folder})" in proc.stderr
    assert "git shift use perso --here" in proc.stderr
    assert "git shift status" in proc.stderr
    assert "git commit --no-verify" in proc.stderr
    monkeypatch.chdir(repo)
    assert main(["use", "perso", "--here"]) == 0
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert head(repo) == EMAIL


def test_outside_every_folder_the_github_owner_decides(home, make_repo):
    repo = outside_every_folder(home, make_repo, "jane")
    proc = commit(repo)
    assert proc.returncode != 0
    assert head(repo) is None
    assert 'this repo belongs to "perso" (jane), but you are committing as other@' in proc.stderr
    assert "(matched by GitHub owner jane)" in proc.stderr


def test_a_repo_of_an_unrelated_owner_passes(home, make_repo):
    repo = outside_every_folder(home, make_repo, "someone")
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert head(repo) == "other@example.com"


def test_a_pin_to_another_identity_wins_over_the_folder(home, repo, monkeypatch):
    add(home, "work", "work@example.com", "work")
    monkeypatch.chdir(repo)
    assert main(["use", "work", "--here"]) == 0
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert head(repo) == "work@example.com"
    blocked = commit(repo, f"--author=Perso <{EMAIL}>")  # the folder's identity
    assert blocked.returncode != 0
    assert '"work"' in blocked.stderr
    assert "(matched by pin in .git/config)" in blocked.stderr


def test_an_email_hand_edited_in_angle_brackets_still_matches(home, repo):
    git(home, "config", "--file", str(store.identity_path("perso")), "user.email", f"<{EMAIL}>")
    assert store.load("perso").email == f"<{EMAIL}>"
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert head(repo) == EMAIL  # git drops the brackets


# what git hands the hook


def test_amend_keeps_the_wrong_author_until_reset_author(repo):
    assert commit(repo, "--no-verify", f"--author=Wrong <{WRONG}>").returncode == 0
    before = head(repo, "%H")
    proc = commit(repo, "--amend")  # the configured email is right, the author of HEAD is not
    assert proc.returncode != 0
    assert f"you are committing as {WRONG}" in proc.stderr
    assert "amending? add --reset-author" in proc.stderr
    assert head(repo, "%H") == before
    proc = commit(repo, "--amend", "--reset-author")
    assert proc.returncode == 0, proc.stderr
    assert head(repo) == EMAIL


def test_the_author_option_is_checked(repo):
    proc = commit(repo, f"--author={OTHER}")
    assert proc.returncode != 0
    assert head(repo) is None
    assert "you are committing as other@example.com" in proc.stderr
    assert "using --author? drop it" in proc.stderr


# bypasses


def test_no_verify_bypasses_the_check(repo):
    git(repo, "config", "user.email", WRONG)
    proc = commit(repo, "--no-verify")
    assert proc.returncode == 0, proc.stderr
    assert head(repo) == WRONG


def test_git_shift_skip_skips_the_check_but_not_the_repository_hook(repo, monkeypatch):
    git(repo, "config", "user.email", WRONG)
    write_script(repo / ".git" / "hooks", "pre-commit", "echo ran > ran.txt")
    monkeypatch.setenv("GIT_SHIFT_SKIP", "1")
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert head(repo) == WRONG
    assert (repo / "ran.txt").exists()


# failing open


def test_a_git_shift_error_lets_the_commit_through_with_a_warning(home, repo):
    broken = add(home, "broken", "broken@example.com", "elsewhere")  # git never reads it here
    with broken.path.open("a", encoding="utf-8") as file:
        file.write("[oops\n")
    git(repo, "config", "user.email", WRONG)  # the check would block
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert head(repo) == WRONG
    lines = proc.stderr.splitlines()
    assert lines[0] == SKIPPED
    assert len(lines) == 2
    assert lines[1].startswith("  `git config --file ")
    assert "broken.gitconfig" in lines[1]


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (RuntimeError("boom"), ["  RuntimeError: boom"]),
        (
            GitShiftError(
                "git failed here:\nfatal: detected dubious ownership in repository at 'x'\n"
                "To add an exception for this directory, call:\n\n"
                "\tgit config --global --add safe.directory x\n"
            ),
            [
                "  git failed here:",
                "  fatal: detected dubious ownership in repository at 'x'",
                "  To add an exception for this directory, call:",
                "",
                "  \tgit config --global --add safe.directory x",
            ],
        ),
    ],
)
def test_any_error_in_the_check_fails_open_with_a_readable_warning(
    home, monkeypatch, capsys, error, shown
):
    def fail():
        raise error

    monkeypatch.setattr(store, "list_identities", fail)
    assert guard.run_hook("pre-commit", [], io.StringIO()) == 0
    assert capsys.readouterr().err.splitlines() == [SKIPPED, *shown]


# the hooks around the check


def test_the_repository_hook_runs_after_a_passing_check(repo):
    write_script(repo / ".git" / "hooks", "pre-commit", "echo ran > ran.txt")
    proc = commit(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert head(repo) == EMAIL
    assert (repo / "ran.txt").exists()


def test_a_linked_worktree_is_checked_then_runs_the_repository_hook(home, repo):
    assert commit(repo).returncode == 0
    worktree = home / "code" / "app-feature"  # its .git is a file: the hook asks git
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    write_script(repo / ".git" / "hooks", "pre-commit", "echo ran > ran.txt")
    blocked = commit(worktree, f"--author={OTHER}")
    assert blocked.returncode != 0
    assert 'this repo belongs to "perso"' in blocked.stderr
    assert not (worktree / "ran.txt").exists()
    proc = commit(worktree)
    assert proc.returncode == 0, proc.stderr
    assert (worktree / "ran.txt").exists()


def test_hook_dispatch_ignores_other_hooks_and_stays_out_of_help(home, capsys):
    assert main(["__hook"]) == 0
    assert main(["__hook", "post-commit", "x"]) == 0
    assert main(["__hook", "pre-commit"]) == 0  # no identities: nothing to check
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit) as exc:
        main(["-h"])
    assert exc.value.code == 0
    assert "__hook" not in capsys.readouterr().out
