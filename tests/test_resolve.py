import os
import re

import pytest
from helpers import git

from git_shift import paths, resolve, store


def add(home, name="perso", folder="code", gh=None, ssh_key=None):
    identity, _ = store.add_identity(
        name,
        email=f"{name}@example.com",
        user_name=name.title(),
        folder=str(home / folder),
        gh=gh,
        ssh_key=ssh_key,
    )
    return identity


def recorded_email(repo, env=None):
    """The email git puts in a commit made in repo."""
    return re.search(r"<(.*)>", git(repo, "var", "GIT_AUTHOR_IDENT", env=env)).group(1)


def test_expected_identity_picks_deepest_folder(home):
    add(home, "perso", "code")
    add(home, "pro", "code/pro")
    identities = store.list_identities()
    assert resolve.expected_identity(home / "code" / "pro" / "x", identities).name == "pro"
    assert resolve.expected_identity(home / "code" / "y", identities).name == "perso"
    assert resolve.expected_identity(home / "elsewhere", identities) is None


def test_expected_identity_breaks_ties_like_git(home, make_repo):
    # after a hand edit two identities can share a folder (here up to case). git applies
    # the rules in write_rules order, (depth, name), so the last name wins.
    add(home, "perso", "code")
    pro = add(home, "pro", "work")
    git(home, "config", "--file", str(pro.path), "gitshift.folder", str(home / "CODE"))
    store.write_rules(store.list_identities())
    repo = make_repo(home / "code" / "app")
    assert git(repo, "config", "user.email") == "pro@example.com"
    assert resolve.expected_identity(repo, store.list_identities()).name == "pro"


def test_remote_identity_matches_github_owner(home):
    add(home, gh="mounimb")
    identities = store.list_identities()
    assert resolve.remote_identity("git@github.com:MounimB/x.git", identities).name == "perso"
    assert resolve.remote_identity("https://gitlab.com/mounimb/x", identities) is None
    assert resolve.remote_identity("https://github.com/someone/x", identities) is None


def test_remote_identity_knows_other_github_hosts(home):
    add(home, gh="mounimb")
    identities = store.list_identities()
    over_443 = "ssh://git@ssh.github.com:443/mounimb/x.git"  # GitHub's SSH over HTTPS port
    assert resolve.remote_identity(over_443, identities).name == "perso"
    assert resolve.remote_identity("https://www.github.com/mounimb/x", identities).name == "perso"


def test_identity_without_email_is_ignored(home, make_repo):
    # store.load gives email="" for a hand-edited file without user.email. Expecting it
    # would block every commit, so it never matches, like an identity without a folder.
    add(home, "perso", "code")
    broken = add(home, "pro", "code/pro", gh="mounimb")
    git(home, "config", "--file", str(broken.path), "--unset", "user.email")
    identities = store.list_identities()
    assert [i.email for i in identities if i.name == "pro"] == [""]
    repo = make_repo(home / "code" / "pro" / "x")
    assert git(repo, "config", "user.email") == "perso@example.com"  # what git applies
    assert resolve.expected_identity(repo, identities).name == "perso"
    assert resolve.remote_identity("git@github.com:mounimb/x.git", identities) is None


def test_expected_for_repo_prefers_folder_then_remote(home, make_repo):
    add(home, "perso", "code", gh="mounimb")
    identities = store.list_identities()
    inside = make_repo(home / "code" / "app")
    outside = make_repo(home / "Desktop" / "x", remote="https://github.com/mounimb/x.git")
    unrelated = make_repo(home / "Desktop" / "y", remote="https://github.com/someone/y.git")
    assert resolve.expected_for_repo(inside, identities)[1].startswith("folder ")
    assert resolve.expected_for_repo(outside, identities)[1] == "GitHub owner mounimb"
    assert resolve.expected_for_repo(unrelated, identities) is None


def test_remote_check_sees_insteadof_rewrites_like_pre_push(home, make_repo):
    add(home, "perso", "code", gh="mounimb")
    git(home, "config", "--global", "url.git@github.com:.insteadOf", "gh:")
    repo = make_repo(home / "Desktop" / "x", remote="gh:mounimb/x.git")
    assert resolve.expected_for_repo(repo, store.list_identities())[1] == "GitHub owner mounimb"


def test_remote_check_follows_pushurl_like_pre_push(home, make_repo):
    add(home, "perso", "code", gh="mounimb")
    repo = make_repo(home / "Desktop" / "x", remote="https://example.com/mirror/x.git")
    git(repo, "remote", "set-url", "--push", "origin", "git@github.com:mounimb/x.git")
    assert resolve.expected_for_repo(repo, store.list_identities())[1] == "GitHub owner mounimb"


def test_worktree_expects_the_identity_git_applies(home, make_repo):
    # git matches includeIf gitdir against the git directory, which for a worktree
    # lives inside the main clone. git-shift must expect the same identity.
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    git(repo, "commit", "--allow-empty", "-q", "-m", "init")
    worktree = home / "work" / "wt"
    git(repo, "worktree", "add", "-q", str(worktree))
    assert git(worktree, "config", "user.email") == "perso@example.com"
    assert resolve.expected_for_repo(worktree, store.list_identities())[0].name == "perso"


def test_pin_beats_folder(home, make_repo):
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "--local", resolve.PIN_KEY, "pro")
    assert resolve.pinned_name(repo) == "pro"
    identity, reason = resolve.expected_for_repo(repo, store.list_identities())
    assert (identity.name, reason) == ("pro", "pin (git shift use --here)")


def test_pin_beats_remote(home, make_repo):
    add(home, "perso", "code", gh="mounimb")
    add(home, "pro", "work")
    url = "git@github.com:mounimb/x.git"
    repo = make_repo(home / "Desktop" / "x", remote=url)
    git(repo, "config", "--local", resolve.PIN_KEY, "pro")
    identities = store.list_identities()
    assert resolve.expected_for_repo(repo, identities)[0].name == "pro"
    assert resolve.expected_for_repo(repo, identities, remote_url=url)[0].name == "pro"  # pre-push


def test_pin_applies_to_worktrees(home, make_repo):
    # use --here writes the shared config, which every worktree of the clone reads
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    git(repo, "commit", "--allow-empty", "-q", "-m", "init")
    git(repo, "config", "--local", resolve.PIN_KEY, "pro")
    worktree = home / "code" / "wt"
    git(repo, "worktree", "add", "-q", str(worktree))
    assert resolve.expected_for_repo(worktree, store.list_identities())[0].name == "pro"


@pytest.mark.parametrize("pin", ["gone", "pro", ""])
def test_dangling_pin_is_ignored(home, make_repo, pin):
    # a removed identity, one without an email (hand-edited), or an empty value
    add(home, "perso", "code")
    broken = add(home, "pro", "work")
    git(home, "config", "--file", str(broken.path), "--unset", "user.email")
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "--local", resolve.PIN_KEY, pin)
    assert resolve.pinned_name(repo) == (pin or None)
    identity, reason = resolve.expected_for_repo(repo, store.list_identities())
    assert (identity.name, reason) == ("perso", f"folder {identity.folder}")


def test_pin_outside_a_repository_is_no_error(home):
    add(home, "perso", "code")
    (home / "code").mkdir()
    assert resolve.pinned_name(home / "code") is None
    assert resolve.expected_for_repo(home / "code", store.list_identities())[0].name == "perso"


def test_pin_in_the_global_config_is_ignored(home, make_repo):
    # only the repository's own config can pin it
    add(home, "perso", "code")
    add(home, "pro", "work")
    git(home, "config", "--global", resolve.PIN_KEY, "pro")
    repo = make_repo(home / "code" / "app")
    assert resolve.pinned_name(repo) is None
    assert resolve.expected_for_repo(repo, store.list_identities())[0].name == "perso"


def test_effective_from_identity(home, make_repo):
    identity = add(home, ssh_key=str(home / ".ssh" / "id_perso"))  # need not exist
    repo = make_repo(home / "code" / "app")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source, eff.identity) == ("perso@example.com", "identity", "perso")
    assert (eff.key, eff.name) == ("user.email", "Perso")
    assert eff.ssh_command == store.ssh_command(identity.ssh_key)
    assert eff.origin == paths.to_git_path(identity.path)


def test_effective_ssh_command_variable_wins(home, make_repo, monkeypatch):
    add(home, ssh_key=str(home / ".ssh" / "id_perso"))
    repo = make_repo(home / "code" / "app")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i other")
    assert resolve.effective(repo, store.list_identities()).ssh_command == "ssh -i other"


def test_effective_local_override(home, make_repo):
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "user.email", "other@example.com")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source) == ("other@example.com", "local")


def test_effective_local_override_from_a_subdirectory(home, make_repo):
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "user.email", "other@example.com")
    subdir = repo / "src" / "deep"
    subdir.mkdir(parents=True)
    assert resolve.effective(subdir, store.list_identities()).source == "local"


def test_effective_local_override_in_a_worktree(home, make_repo):
    # a worktree reads the main clone's config, not one in its own git directory
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "commit", "--allow-empty", "-q", "-m", "init")
    git(repo, "config", "user.email", "other@example.com")
    worktree = home / "elsewhere" / "wt"
    git(repo, "worktree", "add", "-q", str(worktree))
    assert resolve.effective(worktree, store.list_identities()).source == "local"


def test_effective_command_line_value(home, make_repo, monkeypatch):
    add(home)
    repo = make_repo(home / "code" / "app")
    # what `git -c user.email=... shift status` passes on to git-shift's own git calls
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'user.email'='cli@example.com'")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source, eff.origin) == ("cli@example.com", "command line", None)


def test_effective_global_set_after_include_wins(home, make_repo):
    add(home)
    git(home, "config", "--global", "user.email", "global@example.com")
    repo = make_repo(home / "code" / "app")
    assert resolve.effective(repo, store.list_identities()).source == "global"


def test_effective_origin_is_normalised(home, make_repo, monkeypatch):
    config = home / "alt.gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.fspath(config))  # backslashes on Windows
    git(home, "config", "--global", "user.email", "alt@example.com")
    eff = resolve.effective(make_repo(home / "plain"), [])
    assert (eff.source, eff.origin) == ("global", paths.to_git_path(config))


def test_effective_global_author_email_beats_identity(home, make_repo):
    # author.email wins over user.email wherever it is, so git commits with it
    add(home)
    git(home, "config", "--global", "author.email", "author@example.com")
    repo = make_repo(home / "code" / "app")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source, eff.key) == ("author@example.com", "global", "author.email")
    assert eff.identity is None


def test_effective_skips_empty_author_email(home, make_repo):
    add(home)
    git(home, "config", "--global", "author.email", "")
    repo = make_repo(home / "code" / "app")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source, eff.key) == ("perso@example.com", "identity", "user.email")


def test_effective_environment_override(home, make_repo, monkeypatch):
    add(home)
    repo = make_repo(home / "code" / "app")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "env@example.com")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source) == ("env@example.com", "environment")
    assert eff.key == "GIT_AUTHOR_EMAIL"


def test_effective_empty_author_email_variable_still_wins(home, make_repo, monkeypatch):
    add(home)
    repo = make_repo(home / "code" / "app")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "")
    # git commits with an empty email
    assert recorded_email(repo, env={**os.environ, "GIT_AUTHOR_EMAIL": ""}) == ""
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source, eff.key) == ("", "environment", "GIT_AUTHOR_EMAIL")


def test_effective_email_variable_when_nothing_is_configured(home, make_repo, monkeypatch):
    monkeypatch.setenv("EMAIL", "env@example.com")
    eff = resolve.effective(make_repo(home / "plain"), [])
    assert (eff.email, eff.source, eff.key) == ("env@example.com", "environment", "EMAIL")


def test_effective_email_variable_loses_to_identity(home, make_repo, monkeypatch):
    add(home)
    repo = make_repo(home / "code" / "app")
    monkeypatch.setenv("EMAIL", "env@example.com")
    eff = resolve.effective(repo, store.list_identities())
    assert (eff.email, eff.source, eff.key) == ("perso@example.com", "identity", "user.email")


@pytest.mark.parametrize(("key", "value"), [("author.email", ""), ("committer.email", "c@x.io")])
def test_effective_email_variable_ignored_once_any_email_is_configured(
    home, make_repo, monkeypatch, key, value
):
    # git reads EMAIL only when the config sets no email key at all, even an empty one
    git(home, "config", "--global", "user.name", "Tester")  # so git var never guesses one
    git(home, "config", "--global", key, value)
    repo = make_repo(home / "plain")
    monkeypatch.setenv("EMAIL", "env@example.com")
    assert "env@example.com" not in git(repo, "var", "GIT_AUTHOR_IDENT")
    assert resolve.effective(repo, []).source == "unset"


@pytest.mark.parametrize("raw", ["<u@x.io>", " u@x.io ", "'u@x.io',", "u<@x>.io", "u@x.io\\"])
def test_effective_cleans_the_email_like_git(home, make_repo, raw):
    # status compares this email with the identity's, and the guard reads git var
    repo = make_repo(home / "plain")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", raw)
    assert recorded_email(repo) == "u@x.io"
    assert resolve.effective(repo, []).email == "u@x.io"


@pytest.mark.parametrize("variable", ["GIT_AUTHOR_EMAIL", "EMAIL"])
def test_effective_cleans_email_variables_like_git(home, make_repo, monkeypatch, variable):
    repo = make_repo(home / "plain")
    git(repo, "config", "user.name", "Tester")
    monkeypatch.setenv(variable, " <e@x.io>; ")
    assert recorded_email(repo) == "e@x.io"
    assert resolve.effective(repo, []).email == "e@x.io"


def test_effective_name_follows_git_precedence(home, make_repo, monkeypatch):
    add(home)
    repo = make_repo(home / "code" / "app")
    assert resolve.effective(repo, store.list_identities()).name == "Perso"
    git(home, "config", "--global", "author.name", "Author")
    assert resolve.effective(repo, store.list_identities()).name == "Author"
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env")
    assert resolve.effective(repo, store.list_identities()).name == "Env"


def test_effective_unset(home, make_repo):
    eff = resolve.effective(make_repo(home / "plain"), [])
    assert (eff.source, eff.email, eff.key) == ("unset", None, None)
