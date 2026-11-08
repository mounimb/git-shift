"""The pre-push check in real pushes to local bare remotes: git runs the guard hooks, which
run this Python and the real check. Nothing is ever pushed to the network."""

import itertools
import shlex

import pytest
from helpers import git, run, write_script

from git_shift import guard, store
from git_shift.cli import main
from git_shift.errors import GitShiftError

EMAIL = "me@example.com"
WRONG = "wrong@example.com"
OTHER = "other@example.com"
ZERO = "0" * 40
SKIPPED = "git-shift: warning: guard skipped after an error:"
REAUTHOR = "git commit --amend --reset-author --no-edit"
_files = itertools.count()


def add(home, name, email, folder, gh=None):
    identity, _ = store.add_identity(
        name, email=email, user_name=name.title(), folder=str(home / folder), gh=gh
    )
    return identity


def bare(path):
    git(path.parent, "init", "-q", "--bare", str(path))
    return path


@pytest.fixture
def repo(home, make_repo, tmp_path):
    """A repository in the folder of the identity "perso", with an empty bare remote
    origin, and the guard installed."""
    add(home, "perso", EMAIL, "code")
    guard.install()
    return make_repo(home / "code" / "app", remote=str(bare(tmp_path / "origin.git")))


def commit(repo, email=None):
    """A commit that adds a file, as email (default: the configured one), made without any
    hook. Returns its short sha."""
    name = f"file{next(_files)}.txt"
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", name)
    author = ["-c", "user.name=Someone", "-c", f"user.email={email}"] if email else []
    git(repo, "-c", "core.hooksPath=no-hooks", *author, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "--short", "HEAD")


def push(repo, *args):
    """A real push to origin, which the guard may block."""
    return run(repo, "push", "-q", "origin", *(args or ("main",)))


def published(repo, branch="main"):
    """The sha origin has for branch, or None."""
    out = run(repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
    return out[0] if out else None


def stdin_line(repo, ref="main", remote_sha=ZERO):
    """The line git gives pre-push for a push of ref."""
    return f"refs/heads/{ref} {git(repo, 'rev-parse', ref)} refs/heads/{ref} {remote_sha}\n"


def url(repo):
    return git(repo, "remote", "get-url", "origin")


def merge_from_a_teammate(repo, make_repo, tmp_path, email=OTHER):
    """Fetch two commits by email from another bare remote and merge them. Returns their
    full shas."""
    upstream = bare(tmp_path / "upstream.git")
    teammate = make_repo(tmp_path / "teammate", remote=str(upstream))
    commit(teammate, email)
    commit(teammate, email)
    git(teammate, "push", "-q", "origin", "main")
    git(repo, "remote", "add", "upstream", str(upstream))
    git(repo, "fetch", "-q", "upstream")
    git(repo, "merge", "-q", "--no-edit", "--allow-unrelated-histories", "upstream/main")
    return git(repo, "log", "--format=%H", "upstream/main").split()


def follow_hint(repo, stderr):
    """Run the command of the "then:" line exactly as printed, with POSIX shell quoting."""
    line = next(line for line in stderr.splitlines() if line.startswith("  then:"))
    command = shlex.split(line.split(": ", 2)[2])  # "  then:     on main: <command>"
    assert command[0] == "git"
    git(repo, *command[1:])


# what the check blocks, and how the hints fix it


def test_a_wrong_tip_is_blocked_and_the_fix_then_the_amend_hint_work(repo, monkeypatch):
    commit(repo)
    assert push(repo).returncode == 0
    before = published(repo)
    git(repo, "config", "user.email", WRONG)  # git records the wrong email from now on
    bad = commit(repo)
    proc = push(repo)
    assert proc.returncode != 0
    assert published(repo) == before
    assert proc.stderr.startswith(
        f'✗ git-shift: 1 commit to push is not authored by "perso" <{EMAIL}> (matched by folder '
    )
    lines = proc.stderr.splitlines()
    assert lines[1:5] == [
        f"  {bad}  {WRONG}",
        "  fix:      git shift use perso --here  (why it is wrong: git shift status)",
        f"  then:     on main: {REAUTHOR}",
        "  bypass:   pushing someone else's commits? git push --no-verify",
    ]
    monkeypatch.chdir(repo)
    assert main(["use", "perso", "--here"]) == 0
    follow_hint(repo, proc.stderr)  # through the pre-commit check
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert published(repo) == git(repo, "rev-parse", "HEAD")


def test_when_git_already_records_the_right_email_only_the_commits_need_fixing(repo):
    bad = commit(repo, WRONG)  # say, made before git-shift
    lines = push(repo).stderr.splitlines()
    assert lines[1:4] == [
        f"  {bad}  {WRONG}",
        f"  config:   already right ({EMAIL}): only these commits need re-authoring",
        f"  then:     on main: {REAUTHOR}",
    ]


def test_without_the_configured_email_the_fix_line_stays(repo, monkeypatch, capsys):
    def fail(cwd):
        raise GitShiftError("git var failed")

    commit(repo, WRONG)
    monkeypatch.setattr(guard, "author_email", fail)
    assert guard.check_pre_push(repo, ["origin", url(repo)], stdin_line(repo)) == guard.BLOCK_EXIT
    assert "  fix:      git shift use perso --here  (why it is wrong: git shift status)\n" in (
        capsys.readouterr().err
    )


def test_several_wrong_commits_are_blocked_and_the_rebase_hint_fixes_them(repo):
    base = commit(repo)
    first = commit(repo, WRONG)
    commit(repo)
    last = commit(repo, WRONG)
    proc = push(repo)
    assert proc.returncode != 0
    assert published(repo) is None
    lines = proc.stderr.splitlines()
    assert lines[0].startswith('✗ git-shift: 2 commits to push are not authored by "perso" ')
    assert lines[1:3] == [f"  {last}  {WRONG}", f"  {first}  {WRONG}"]  # newest first
    assert f'  then:     on main: git rebase -r --exec "{REAUTHOR}" {base}' in lines
    assert f"            this re-authors every commit after {base}" in lines
    git(repo, "rebase", "-q", "-r", "--exec", REAUTHOR, base)  # as shown
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert git(repo, "log", "--format=%ae").splitlines() == [EMAIL] * 4


def test_wrong_commits_back_to_the_root_get_a_root_rebase_hint(repo):
    commit(repo, WRONG)
    commit(repo, WRONG)
    lines = push(repo).stderr.splitlines()
    assert f'  then:     on main: git rebase -r --exec "{REAUTHOR}" --root' in lines
    assert "            this re-authors every commit of main" in lines
    git(repo, "rebase", "-q", "-r", "--exec", REAUTHOR, "--root")  # as shown
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr


def test_the_rebase_hint_leaves_merged_commits_of_others_alone(repo, make_repo, tmp_path):
    base = commit(repo)
    assert push(repo).returncode == 0
    bad = commit(repo, WRONG)
    theirs = merge_from_a_teammate(repo, make_repo, tmp_path)  # after base, never checked
    proc = push(repo)
    assert proc.returncode != 0
    lines = proc.stderr.splitlines()
    assert lines[0].startswith("✗ git-shift: 1 commit to push is not authored by ")
    assert lines[1] == f"  {bad}  {WRONG}"
    reauthor = f"if git log -1 --format=%ae | grep -qixF -e {WRONG}; then {REAUTHOR}; fi"
    assert f'  then:     on main: git rebase -r --exec "{reauthor}" {base}' in lines
    assert f"            it re-authors only the commits after {base} by the emails above" in lines
    follow_hint(repo, proc.stderr)
    authors = dict(line.split(" ") for line in git(repo, "log", "--format=%H %ae").splitlines())
    assert [authors.get(sha) for sha in theirs] == [OTHER, OTHER]  # same shas, same author
    assert WRONG not in authors.values()
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr


def test_an_email_the_rebase_hint_cannot_quote_falls_back_with_a_warning(repo, make_repo, tmp_path):
    base = commit(repo)
    assert push(repo).returncode == 0
    commit(repo, "o'neil@example.com")  # a quote: no shell-safe condition
    merge_from_a_teammate(repo, make_repo, tmp_path)
    lines = push(repo).stderr.splitlines()
    assert f'  then:     on main: git rebase -r --exec "{REAUTHOR}" {base}' in lines
    assert (
        f"            this re-authors every commit after {base}, even ones that are not yours"
        in lines
    )


def test_a_tag_gets_a_hint_it_can_follow(repo):
    commit(repo)
    assert push(repo).returncode == 0
    bad = commit(repo, WRONG)
    git(repo, "tag", "-a", "v1", "-m", "v1")  # an annotated tag: git hands over its own sha
    proc = push(repo, "v1")
    assert proc.returncode != 0
    assert run(repo, "ls-remote", "origin", "refs/tags/v1").stdout == ""
    lines = proc.stderr.splitlines()
    assert lines[1] == f"  {bad}  {WRONG}"
    assert (
        "  then:     re-author them on their branch, then move the tag (git tag -f v1 <commit>)"
        in lines
    )


def test_a_ref_that_is_no_branch_nor_tag_gets_a_general_hint(repo, capsys):
    commit(repo, WRONG)
    stdin = f"refs/custom/x {git(repo, 'rev-parse', 'HEAD')} refs/custom/x {ZERO}\n"
    assert guard.check_pre_push(repo, ["origin", url(repo)], stdin) == guard.BLOCK_EXIT
    assert "  then:     re-author them on a branch, then point refs/custom/x at them\n" in (
        capsys.readouterr().err
    )


def test_more_than_ten_wrong_commits_are_cut_short(repo):
    bad = [commit(repo, WRONG) for _ in range(12)]
    lines = push(repo).stderr.splitlines()
    assert lines[0].startswith("✗ git-shift: 12 commits to push are not authored by ")
    shown = [f"  {sha}  {WRONG}" for sha in reversed(bad[2:])]
    assert lines[1:12] == [*shown, "  … and 2 more"]


def test_right_authors_pass(repo):
    commit(repo)
    commit(repo)
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert published(repo) == git(repo, "rev-parse", "HEAD")


# which commits are checked


def test_commits_already_published_are_not_checked_again(repo):
    commit(repo, WRONG)
    assert push(repo, "--no-verify", "main").returncode == 0  # as before the guard existed
    commit(repo)
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""


def test_a_new_branch_is_checked_only_for_commits_no_remote_branch_has(repo):
    commit(repo, WRONG)
    assert push(repo, "--no-verify", "main").returncode == 0
    git(repo, "switch", "-q", "-c", "feature")
    commit(repo)
    bad = commit(repo, WRONG)
    proc = push(repo, "feature")
    assert proc.returncode != 0
    assert "✗ git-shift: 1 commit to push is not authored" in proc.stderr
    assert f"\n  {bad}  {WRONG}\n" in proc.stderr
    assert "  then:     on feature: " in proc.stderr


def test_someone_elses_commits_fetched_and_merged_do_not_block(repo, make_repo, tmp_path):
    upstream = bare(tmp_path / "upstream.git")
    teammate = make_repo(tmp_path / "teammate", remote=str(upstream))
    commit(teammate, OTHER)
    commit(teammate, OTHER)
    git(teammate, "push", "-q", "origin", "main")
    commit(repo)
    git(repo, "remote", "add", "upstream", str(upstream))
    git(repo, "fetch", "-q", "upstream")
    git(repo, "merge", "-q", "--no-edit", "--allow-unrelated-histories", "upstream/main")
    proc = push(repo)  # publishes the teammate's commits to origin for the first time
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert published(repo) == git(repo, "rev-parse", "HEAD")


def test_deleting_a_remote_branch_passes(repo):
    commit(repo, WRONG)
    assert push(repo, "--no-verify", "main:gone").returncode == 0
    proc = push(repo, ":gone")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert published(repo, "gone") is None


def test_a_branch_name_the_locale_cannot_decode_is_still_checked(repo):
    branch = "brÁnch"  # UTF-8 C3 81, and 0x81 means nothing in cp1252
    git(repo, "switch", "-q", "-c", branch)
    commit(repo, WRONG)
    proc = push(repo, branch)
    assert proc.returncode != 0, proc.stderr
    assert f"  then:     on {branch}: {REAUTHOR}" in proc.stderr.splitlines()


def test_commits_of_several_refs_count_once_and_the_hint_is_for_the_first(repo, capsys):
    commit(repo)
    commit(repo, WRONG)  # the tip of main
    git(repo, "switch", "-q", "-c", "feature")
    commit(repo, WRONG)
    stdin = stdin_line(repo, "main") + stdin_line(repo, "feature")
    assert guard.check_pre_push(repo, ["origin", url(repo)], stdin) == guard.BLOCK_EXIT
    err = capsys.readouterr().err
    assert "✗ git-shift: 2 commits to push are not authored" in err
    assert f"  then:     on main: {REAUTHOR}\n" in err


def test_a_remote_sha_unknown_here_still_checks_the_new_commits(repo, capsys):
    bad = commit(repo, WRONG)
    stdin = stdin_line(repo, remote_sha="1" * 40)  # pushed by someone, never fetched here
    assert guard.check_pre_push(repo, ["origin", url(repo)], stdin) == guard.BLOCK_EXIT
    assert f"\n  {bad}  {WRONG}\n" in capsys.readouterr().err


def test_input_git_never_sends_is_an_error(repo):
    with pytest.raises(GitShiftError, match="unexpected pre-push input"):
        guard.check_pre_push(repo, ["origin", url(repo)], "refs/heads/main not-a-sha\n")


# which identity is expected


def test_an_email_hand_edited_in_angle_brackets_still_matches(home, repo):
    git(home, "config", "--file", str(store.identity_path("perso")), "user.email", f"<{EMAIL}>")
    commit(repo)
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""


def test_a_pinned_repo_expects_its_pin(home, repo, monkeypatch):
    add(home, "work", "work@example.com", "work")
    monkeypatch.chdir(repo)
    assert main(["use", "work", "--here"]) == 0
    commit(repo)  # as work
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    bad = commit(repo, EMAIL)  # as perso, the identity of the folder
    proc = push(repo)
    assert proc.returncode != 0
    assert 'not authored by "work" <work@example.com> (matched by pin in .git/config)' in (
        proc.stderr
    )
    assert f"\n  {bad}  {EMAIL}\n" in proc.stderr


def test_outside_every_folder_the_owner_of_the_push_url_decides(home, make_repo, capsys):
    add(home, "perso", EMAIL, "code", gh="jane")
    repo = make_repo(home / "elsewhere" / "x")  # no origin: only the push URL names an owner
    commit(repo, OTHER)
    stdin = stdin_line(repo)
    owned = ["origin", "https://github.com/jane/x.git"]
    assert guard.check_pre_push(repo, owned, stdin) == guard.BLOCK_EXIT
    assert "(matched by GitHub owner jane)" in capsys.readouterr().err
    unrelated = ["origin", "https://github.com/someone/x.git"]
    assert guard.check_pre_push(repo, unrelated, stdin) == 0
    assert capsys.readouterr().err == ""


# bypasses and failing open


def test_no_verify_bypasses_the_check(repo):
    commit(repo, WRONG)
    proc = push(repo, "--no-verify", "main")
    assert proc.returncode == 0, proc.stderr
    assert published(repo) == git(repo, "rev-parse", "HEAD")


def test_git_shift_skip_skips_the_check_but_not_the_repository_hook(repo, monkeypatch):
    commit(repo, WRONG)
    write_script(repo / ".git" / "hooks", "pre-push", "cat > pushed.txt")
    monkeypatch.setenv("GIT_SHIFT_SKIP", "1")
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert (repo / "pushed.txt").read_text(encoding="utf-8") == stdin_line(repo)


def test_a_git_shift_error_lets_the_push_through_with_a_warning(home, repo):
    broken = add(home, "broken", "broken@example.com", "elsewhere")  # git never reads it here
    with broken.path.open("a", encoding="utf-8") as file:
        file.write("[oops\n")
    commit(repo, WRONG)  # the check would block
    proc = push(repo)
    assert proc.returncode == 0, proc.stderr
    assert published(repo) == git(repo, "rev-parse", "HEAD")
    assert proc.stderr.startswith(f"{SKIPPED}\n  `git config --file ")
