import shlex

import pytest
from helpers import git, make_git_refuse, write_fake_gh

from git_shift import gh, gitconfig, paths, resolve, store
from git_shift.cli import main


def add(home, name="perso", folder="code", *extra):
    args = ["add", name, "--email", f"{name}@example.com", "--name", name.title()]
    assert main([*args, "--folder", str(home / folder), *extra]) == 0


def run_printed_fix(out, cwd):
    """Run the first `fix:` line of out as a shell would: a printed fix has to work."""
    line = next(line for line in out.splitlines() if line.startswith("  fix: "))
    program, *args = shlex.split(line.removeprefix("  fix: "))
    assert program == "git"
    git(cwd, *args)  # raises if git fails


def test_status_in_identity_folder(home, make_repo, monkeypatch, capsys):
    add(home)
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    capsys.readouterr()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert 'expected: "perso"' in out
    assert 'perso@example.com  (from identity "perso")' in out


def test_status_local_override_exits_1_with_fix(home, make_repo, monkeypatch, capsys):
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "user.email", "wrong@example.com")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "local config" in out
    assert "git shift use perso --here" in out


def test_status_remote_rule_outside_folders(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code", "--gh", "mounimb")
    git(home, "config", "--global", "user.email", "other@example.com")
    monkeypatch.chdir(make_repo(home / "Desktop" / "x", remote="git@github.com:mounimb/x.git"))
    assert main(["status"]) == 1
    assert "GitHub owner mounimb" in capsys.readouterr().out


def test_status_does_not_match_a_folder_git_does_not(home, make_repo, monkeypatch, capsys):
    # str.casefold makes these equal, gitdir/i does not: exit 1 would ask for a pointless fix
    add(home, "perso", "straße")
    repo = make_repo(home / "STRASSE" / "app")
    assert gitconfig.get("user.email", cwd=repo) is None
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 0
    assert "expected: no identity matches this location" in capsys.readouterr().out


def test_status_outside_repo(home, monkeypatch, capsys):
    monkeypatch.chdir(home)
    assert main(["status"]) == 0
    assert "(not a git repository)" in capsys.readouterr().out


# --- git refuses the repository: that is not "no repository" ------------------------------


def test_status_when_git_refuses_the_repository(home, make_repo, monkeypatch, capsys):
    add(home)
    repo = make_repo(home / "code" / "app")
    make_git_refuse(repo)
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("git-shift: error: ")
    assert "unknown repository extension found:\n\tfoo" in captured.err  # every line
    assert "not a git repository" not in captured.out + captured.err


def test_use_here_when_git_refuses_the_repository(home, make_repo, monkeypatch, capsys):
    add(home)
    repo = make_repo(home / "code" / "app")
    make_git_refuse(repo)
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["use", "perso", "--here"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("git-shift: error: ")
    assert "unknown repository extension found:\n\tfoo" in captured.err
    assert "not inside a git repository" not in captured.err
    assert gitconfig.get(resolve.PIN_KEY, file=repo / ".git" / "config") is None


def test_status_with_a_valueless_email_in_the_repo_config(home, make_repo, monkeypatch, capsys):
    add(home)
    repo = make_repo(home / "code" / "app")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as config:
        config.write("[user]\n\temail\n")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 2
    err = capsys.readouterr().err
    assert "missing value for 'user.email'" in err
    assert "bad config variable 'user.email'" in err


def test_status_prints_the_fix_git_gives_for_dubious_ownership(
    home, make_repo, monkeypatch, capsys
):
    add(home)
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    capsys.readouterr()
    assert main(["status"]) == 2
    err = capsys.readouterr().err
    assert "detected dubious ownership" in err
    assert "git config --global --add safe.directory" in err


def test_use_here_pins_identity(home, make_repo, monkeypatch):
    add(home, "pro", "work")
    repo = make_repo(home / "Desktop" / "y")
    monkeypatch.chdir(repo)
    assert main(["use", "pro", "--here"]) == 0
    assert git(repo, "config", "--local", "user.email") == "pro@example.com"


def test_use_without_here_is_rejected(home, capsys):
    add(home, "pro", "work")
    assert main(["use", "pro"]) == 2
    assert "--here" in capsys.readouterr().err


# --- the real cause of a wrong email, and the fix that works -----------------------------


def test_status_remote_rule_explains_the_folder_and_the_fix(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code", "--gh", "mounimb")
    git(home, "config", "--global", "user.email", "other@example.com")
    monkeypatch.chdir(make_repo(home / "Desktop" / "x", remote="git@github.com:mounimb/x.git"))
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    folder = paths.to_folder_rule(home / "code")
    assert f"✗ this repo belongs to mounimb on GitHub but is outside {folder}" in out
    assert "  fix: git shift use perso --here" in out


@pytest.mark.parametrize(
    ("scope", "place"),
    [("global", "your global git config"), ("local", "this repo's .git/config")],
)
def test_status_multi_valued_author_email_has_a_fix_that_works(
    home, make_repo, monkeypatch, capsys, scope, place
):
    # author.email beats user.email in every scope, so use --here would not help. Several
    # values are the hard case: plain --unset exits 5 and changes nothing.
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", f"--{scope}", "--add", "author.email", "one@example.com")
    git(repo, "config", f"--{scope}", "--add", "author.email", "author@example.com")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert f"author@example.com  (from author.email in {place})" in out
    assert f'✗ author.email in {place} replaces the email of "perso"' in out
    assert f"  fix: git config --{scope} --unset-all author.email" in out
    assert "git shift use" not in out
    run_printed_fix(out, repo)
    assert main(["status"]) == 0


def test_status_author_email_in_the_second_global_file_has_a_fix_that_works(
    home, make_repo, monkeypatch, capsys
):
    # git reads both files, but --global writes only ~/.gitconfig once it exists
    add(home)
    assert (home / ".gitconfig").is_file()
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True, exist_ok=True)
    xdg.write_text("[author]\n\temail = xdg@example.com\n", encoding="utf-8")
    repo = make_repo(home / "code" / "app")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "xdg@example.com  (from author.email in your global git config)" in out
    assert f'  fix: git config --file "{paths.to_git_path(xdg)}" --unset-all author.email' in out
    run_printed_fix(out, repo)
    assert gitconfig.get("author.email", cwd=repo) is None
    assert main(["status"]) == 0


def test_status_author_email_in_the_only_global_file_uses_global(
    home, make_repo, monkeypatch, capsys
):
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True, exist_ok=True)
    xdg.write_text("", encoding="utf-8")
    add(home)
    assert not (home / ".gitconfig").exists()  # so --global means the XDG file
    git(home, "config", "--global", "author.email", "xdg@example.com")
    repo = make_repo(home / "code" / "app")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "  fix: git config --global --unset-all author.email" in out
    run_printed_fix(out, repo)
    assert main(["status"]) == 0


def test_status_author_email_in_an_included_file(home, make_repo, monkeypatch, capsys):
    add(home)
    extra = home / "extra.gitconfig"
    extra.write_text("[author]\n\temail = extra@example.com\n", encoding="utf-8")
    git(home, "config", "--global", "include.path", str(extra))
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    capsys.readouterr()
    assert main(["status"]) == 1
    assert f"  fix: remove author.email from {paths.to_git_path(extra)}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("key", "described"),
    [
        ("user.email", "this worktree's config.worktree"),
        ("author.email", "author.email in this worktree's config.worktree"),
    ],
)
def test_status_config_worktree_has_a_fix_that_works(
    home, make_repo, monkeypatch, capsys, key, described
):
    # config.worktree beats .git/config: suggesting use --here would send the user in a loop
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", key, "wt@example.com")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert f"wt@example.com  (from {described})" in out
    assert f"{key} in this worktree's config.worktree replaces the email of" in out
    assert f"  fix: git config --worktree --unset-all {key}" in out
    assert "git shift use" not in out

    assert main(["use", "pro", "--here"]) == 0  # the override still wins, and it says so
    out = capsys.readouterr().out
    assert '! pinned "pro" on this repo, but git still commits as wt@example.com' in out
    run_printed_fix(out, repo)

    assert main(["use", "pro", "--here"]) == 0
    assert '✓ this repo now commits as pro@example.com ("pro")' in capsys.readouterr().out
    assert main(["status"]) == 0


PIN_PRO = [("--local", "gitshift.identity", "pro")]


@pytest.mark.parametrize(
    ("folder", "config", "env", "code", "lines"),
    [
        pytest.param(
            "Desktop/x",
            PIN_PRO,
            {},
            1,
            [
                "email:    (unset)  (from nothing: no email configured)",
                "✗ no email is configured here",
                "  fix: git shift use pro --here",
            ],
            id="unset",
        ),
        pytest.param(
            "code/app",
            [],
            {"GIT_AUTHOR_EMAIL": ""},
            1,
            [
                "email:    (empty)  (from the GIT_AUTHOR_EMAIL environment variable)",
                "✗ GIT_AUTHOR_EMAIL is set in your environment",
                "  fix: unset the GIT_AUTHOR_EMAIL environment variable",
            ],
            id="empty-author-variable",
        ),
        pytest.param(
            "code/app",
            [],
            {"GIT_AUTHOR_EMAIL": "env@example.com"},
            1,
            [
                "email:    env@example.com  (from the GIT_AUTHOR_EMAIL environment variable)",
                "✗ GIT_AUTHOR_EMAIL is set in your environment",
                "  fix: unset the GIT_AUTHOR_EMAIL environment variable",
            ],
            id="author-variable",
        ),
        pytest.param(
            "Desktop/x",
            PIN_PRO,
            {"EMAIL": "env@example.com"},
            1,
            [
                "email:    env@example.com  (from the EMAIL environment variable)",
                '✗ the email comes from the EMAIL environment variable, not from "pro"',
                "  fix: git shift use pro --here",
            ],
            id="email-variable",
        ),
        pytest.param(
            "code/app",
            [],
            {"GIT_CONFIG_PARAMETERS": "'user.email'='cli@example.com'"},
            1,
            [
                "email:    cli@example.com  (from a -c option or GIT_CONFIG_* variable)",
                '✗ a -c user.email option or GIT_CONFIG_* variable replaces the email of "perso"',
                "  fix: drop the -c user.email option or GIT_CONFIG_* variable",
            ],
            id="command-line-user-email",
        ),
        pytest.param(
            "code/app",
            [],
            {"GIT_CONFIG_PARAMETERS": "'author.email'='cli@example.com'"},
            1,
            [
                "(from author.email in a -c option or GIT_CONFIG_* variable)",
                "✗ author.email in a -c option or GIT_CONFIG_* variable replaces the email",
                "  fix: drop the -c author.email option or GIT_CONFIG_* variable",
            ],
            id="command-line-author-email",
        ),
        pytest.param(
            "code/app",
            [("--global", "user.email", "global@example.com")],
            {},
            1,
            [
                "email:    global@example.com  (from your global git config)",
                "✗ your global git config overrides git-shift rules: run git shift doctor",
                "  fix: git shift use perso --here",
            ],
            id="global-beats-a-folder-rule",
        ),
        pytest.param(
            "Desktop/x",
            [*PIN_PRO, ("--global", "user.email", "global@example.com")],
            {},
            1,
            [
                '✗ the email comes from your global git config, not from "pro"',
                "  fix: git shift use pro --here",
            ],
            id="global-with-a-pin",
        ),
        pytest.param(
            "code/app",
            PIN_PRO,
            {},
            1,
            [
                'email:    perso@example.com  (from identity "perso")',
                '✗ the email comes from identity "perso", not from "pro"',
                "  fix: git shift use pro --here",
            ],
            id="generic",
        ),
        pytest.param(
            "Desktop/x",
            [],
            {},
            0,
            [
                "expected: no identity matches this location",
                "email:    (unset)  (from nothing: no email configured)",
            ],
            id="no-identity-in-a-repo",
        ),
    ],
)
def test_status_explains_the_email(
    home, make_repo, monkeypatch, capsys, folder, config, env, code, lines
):
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / folder)
    for args in config:
        git(repo, "config", *args)
    monkeypatch.chdir(repo)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    capsys.readouterr()
    assert main(["status"]) == code
    out = capsys.readouterr().out
    for line in lines:
        assert line in out
    if not any("git shift use" in line for line in lines):  # it could not beat these
        assert "git shift use" not in out


def test_status_shows_the_ssh_command(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code", "--ssh-key", str(home / ".ssh" / "id_perso"))
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    capsys.readouterr()
    assert main(["status"]) == 0
    command = store.ssh_command(paths.to_git_path(home / ".ssh" / "id_perso"))
    assert f"ssh:      {command}\n" in capsys.readouterr().out


def test_status_remote_rule_for_an_identity_without_folder(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code", "--gh", "mounimb")
    path = store.identity_path("perso")
    git(home, "config", "--file", str(path), "--unset", "gitshift.folder")  # hand-edited
    git(home, "config", "--global", "user.email", "other@example.com")
    monkeypatch.chdir(make_repo(home / "Desktop" / "x", remote="git@github.com:mounimb/x.git"))
    capsys.readouterr()
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert '✗ this repo belongs to mounimb on GitHub, but "perso" has no usable folder' in out
    assert "outside" not in out
    assert "  fix: git shift use perso --here" in out


def git_directory_without_a_working_tree(home, make_repo, inside):
    if inside == ".git":
        return make_repo(home / "code" / "app") / ".git"
    directory = home / "code" / "bare.git"
    directory.mkdir(parents=True)
    git(directory, "init", "-q", "--bare")
    return directory


@pytest.mark.parametrize("inside", ["bare", ".git"])
def test_status_without_a_working_tree(home, make_repo, monkeypatch, capsys, inside):
    # a git directory, so "not a git repository" would be wrong; nothing commits from here
    add(home)
    directory = git_directory_without_a_working_tree(home, make_repo, inside)
    monkeypatch.chdir(directory)
    capsys.readouterr()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"repo:     {paths.to_git_path(directory)} (no working tree)\n" in out
    assert 'expected: "perso"' in out
    assert "not a git repository" not in out
    assert "email:" not in out


# --- use --here --------------------------------------------------------------------------


def test_use_here_inside_another_identity_folder_wins(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0
    out = capsys.readouterr().out
    assert '✓ this repo now commits as pro@example.com ("pro")' in out
    assert "!" not in out  # no SSH key anywhere, nothing to warn about
    assert git(repo, "config", "--local", "gitshift.identity") == "pro"
    assert git(repo, "config", "--local", "user.name") == "Pro"
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert 'expected: "pro" <pro@example.com>  (matched by pin in .git/config)' in out
    assert "pro@example.com  (from this repo's .git/config)" in out


def test_use_here_from_a_subdirectory(home, make_repo, monkeypatch, capsys):
    add(home, "pro", "work")
    repo = make_repo(home / "Desktop" / "y")
    subdir = repo / "src" / "deep"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    assert main(["use", "pro", "--here"]) == 0
    assert git(repo, "config", "--local", "user.email") == "pro@example.com"
    assert git(repo, "config", "--local", "gitshift.identity") == "pro"
    assert main(["status"]) == 0
    assert "(matched by pin in .git/config)" in capsys.readouterr().out


def test_use_here_from_a_linked_worktree(home, make_repo, monkeypatch, capsys):
    # the pin goes to the config every worktree of the clone shares
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    git(repo, "commit", "--allow-empty", "-q", "-m", "init")
    linked = home / "code" / "wt"
    git(repo, "worktree", "add", "-q", str(linked))
    monkeypatch.chdir(linked)
    assert main(["use", "pro", "--here"]) == 0
    assert git(repo, "config", "--local", "gitshift.identity") == "pro"
    capsys.readouterr()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "(matched by pin in .git/config)" in out
    assert "pro@example.com  (from this repo's .git/config)" in out


def test_use_here_replaces_every_value(home, make_repo, monkeypatch, capsys):
    # plain `git config` exits 5 on a multi-valued key, which would leave a half-pinned repo
    add(home, "keyed", "code", "--ssh-key", str(home / ".ssh" / "id_keyed"))
    repo = make_repo(home / "Desktop" / "y")
    keys = ("user.name", "user.email", "core.sshCommand", resolve.PIN_KEY)
    for key in keys:
        git(repo, "config", "--local", "--add", key, "one")
        git(repo, "config", "--local", "--add", key, "two")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["use", "keyed", "--here"]) == 0
    assert '✓ this repo now commits as keyed@example.com ("keyed")' in capsys.readouterr().out
    command = store.ssh_command(paths.to_git_path(home / ".ssh" / "id_keyed"))
    assert [gitconfig.get_all(key, scope="local", cwd=repo) for key in keys] == [
        ["Keyed"],
        ["keyed@example.com"],
        [command],
        ["keyed"],
    ]


def test_use_here_writes_the_ssh_command(home, make_repo, monkeypatch):
    add(home, "pro", "work", "--ssh-key", str(home / ".ssh" / "id_pro"))
    repo = make_repo(home / "Desktop" / "y")
    monkeypatch.chdir(repo)
    assert main(["use", "pro", "--here"]) == 0
    key = paths.to_git_path(home / ".ssh" / "id_pro")
    assert git(repo, "config", "--local", "core.sshCommand") == store.ssh_command(key)


def test_use_here_warns_when_a_folder_ssh_key_still_applies(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code", "--ssh-key", str(home / ".ssh" / "id_perso"))
    add(home, "pro", "work")
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0
    out = capsys.readouterr().out
    command = store.ssh_command(paths.to_git_path(home / ".ssh" / "id_perso"))
    assert f'! this repo still uses {command} for SSH, but "pro" has no SSH key' in out
    assert "  fix: git config --local --replace-all core.sshCommand ssh" in out


def test_use_here_warns_about_the_key_of_an_earlier_pin(home, make_repo, monkeypatch, capsys):
    add(home, "keyed", "code", "--ssh-key", str(home / ".ssh" / "id_keyed"))
    add(home, "pro", "work")
    repo = make_repo(home / "Desktop" / "y")
    monkeypatch.chdir(repo)
    assert main(["use", "keyed", "--here"]) == 0
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0
    out = capsys.readouterr().out
    command = store.ssh_command(paths.to_git_path(home / ".ssh" / "id_keyed"))
    assert f'! this repo still uses {command} for SSH, but "pro" has no SSH key' in out
    assert "  fix: git config --local --replace-all core.sshCommand ssh" in out

    run_printed_fix(out, repo)
    assert git(repo, "config", "--local", "core.sshCommand") == "ssh"
    assert main(["use", "pro", "--here"]) == 0  # plain ssh is what "pro" wants: no more warning
    assert "SSH" not in capsys.readouterr().out


def test_use_here_ssh_fix_works_on_several_values(home, make_repo, monkeypatch, capsys):
    add(home, "keyed", "code", "--ssh-key", str(home / ".ssh" / "id_keyed"))
    add(home, "pro", "work")
    repo = make_repo(home / "Desktop" / "y")
    command = store.ssh_command(paths.to_git_path(home / ".ssh" / "id_keyed"))
    git(repo, "config", "--local", "--add", "core.sshCommand", "ssh -i old")
    git(repo, "config", "--local", "--add", "core.sshCommand", command)  # the last one applies
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0
    run_printed_fix(capsys.readouterr().out, repo)
    assert gitconfig.get_all("core.sshCommand", scope="local", cwd=repo) == ["ssh"]


def test_use_here_never_suggests_replacing_the_users_own_ssh_program(
    home, make_repo, monkeypatch, capsys
):
    # git-shift did not write this command: the fix would replace their program in this repo
    program = "C:/Windows/System32/OpenSSH/ssh.exe"
    add(home, "keyed", "code", "--ssh-key", str(home / ".ssh" / "id_keyed"))
    add(home, "pro", "work")
    git(home, "config", "--global", "core.sshCommand", program)
    monkeypatch.chdir(make_repo(home / "Desktop" / "y"))
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0
    out = capsys.readouterr().out
    assert f'! this repo still uses {program} for SSH, but "pro" has no SSH key' in out
    assert "fix:" not in out


@pytest.mark.parametrize(
    ("name", "has"),
    [("keyed", "uses the key {key}"), ("pro", "has no SSH key")],
)
def test_use_here_warns_when_the_ssh_variable_wins(home, make_repo, monkeypatch, capsys, name, has):
    key = home / ".ssh" / "id_keyed"
    add(home, "keyed", "code", "--ssh-key", str(key))
    add(home, "pro", "work")
    monkeypatch.chdir(make_repo(home / "Desktop" / "y"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i other")
    capsys.readouterr()
    assert main(["use", name, "--here"]) == 0
    out = capsys.readouterr().out
    has = has.format(key=paths.to_git_path(key))
    assert f'! this repo still uses ssh -i other for SSH, but "{name}" {has}' in out
    assert "  fix: unset the GIT_SSH_COMMAND environment variable" in out


def test_use_here_never_suggests_overwriting_the_key_it_just_wrote(
    home, make_repo, monkeypatch, capsys
):
    add(home, "keyed", "code", "--ssh-key", str(home / ".ssh" / "id_keyed"))
    monkeypatch.chdir(make_repo(home / "Desktop" / "y"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.sshcommand'='ssh -i other'")
    capsys.readouterr()
    assert main(["use", "keyed", "--here"]) == 0
    out = capsys.readouterr().out
    assert "! this repo still uses ssh -i other for SSH" in out
    assert "fix:" not in out


@pytest.mark.parametrize(
    ("prepare", "env", "described", "fix"),
    [
        pytest.param(
            ("--global", "author.email", "other@example.com"),
            {},
            "author.email in your global git config",
            "git config --global --unset-all author.email",
            id="global-author-email",
        ),
        pytest.param(
            ("--local", "author.email", "other@example.com"),
            {},
            "author.email in this repo's .git/config",
            "git config --local --unset-all author.email",
            id="local-author-email",
        ),
        pytest.param(
            None,
            {"GIT_AUTHOR_EMAIL": "other@example.com"},
            "the GIT_AUTHOR_EMAIL environment variable",
            "unset the GIT_AUTHOR_EMAIL environment variable",
            id="author-variable",
        ),
        pytest.param(
            None,
            {"GIT_CONFIG_PARAMETERS": "'user.email'='other@example.com'"},
            "a -c option or GIT_CONFIG_* variable",
            "drop the -c user.email option or GIT_CONFIG_* variable",
            id="command-line",
        ),
    ],
)
def test_use_here_never_says_ok_when_another_email_wins(
    home, make_repo, monkeypatch, capsys, prepare, env, described, fix
):
    add(home, "pro", "work")
    repo = make_repo(home / "Desktop" / "y")
    if prepare:
        git(repo, "config", *prepare)
    monkeypatch.chdir(repo)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0  # the pin was written
    out = capsys.readouterr().out
    assert "✓" not in out
    still = "but git still commits as other@example.com"
    assert f'! pinned "pro" on this repo, {still} (from {described})' in out
    assert f"  fix: {fix}" in out
    assert git(repo, "config", "--local", "gitshift.identity") == "pro"


def test_use_here_shows_no_fix_when_it_has_none(home, make_repo, monkeypatch, capsys):
    # a file included by .git/config, read after the value use --here writes
    add(home, "pro", "work")
    repo = make_repo(home / "Desktop" / "y")
    extra = home / "extra.gitconfig"
    extra.write_text("[user]\n\temail = extra@example.com\n", encoding="utf-8")
    git(repo, "config", "--local", "user.email", "first@example.com")
    git(repo, "config", "--local", "include.path", str(extra))
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 0
    out = capsys.readouterr().out
    still = "but git still commits as extra@example.com"
    assert f'! pinned "pro" on this repo, {still} (from {paths.to_git_path(extra)})' in out
    assert "fix:" not in out


@pytest.mark.parametrize("key", ["user.email", "user.name"])
def test_use_here_refuses_an_incomplete_identity(home, make_repo, monkeypatch, capsys, key):
    add(home, "pro", "work")
    path = store.identity_path("pro")
    git(home, "config", "--file", str(path), "--unset", key)  # hand-edited
    repo = make_repo(home / "Desktop" / "y")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 2
    err = capsys.readouterr().err
    assert f'identity "pro" has no {key} in {paths.to_git_path(path)}' in err
    for written in ("user.name", "user.email", resolve.PIN_KEY):
        assert gitconfig.get(written, scope="local", cwd=repo) is None


def test_use_here_outside_a_repository(home, capsys):
    add(home, "pro", "work")
    assert main(["use", "pro", "--here"]) == 2
    assert "not inside a git repository" in capsys.readouterr().err


@pytest.mark.parametrize("inside", ["bare", ".git"])
def test_use_here_without_a_working_tree(home, make_repo, monkeypatch, capsys, inside):
    add(home, "pro", "work")
    directory = git_directory_without_a_working_tree(home, make_repo, inside)
    monkeypatch.chdir(directory)
    capsys.readouterr()
    assert main(["use", "pro", "--here"]) == 2
    err = capsys.readouterr().err
    assert "no working tree" in err
    assert "not inside a git repository" not in err
    assert gitconfig.get(resolve.PIN_KEY, scope="local", cwd=directory) is None


@pytest.mark.parametrize(
    ("pin", "problem"),
    [("gone", "which does not exist"), ("pro", "which has no email, so the pin is ignored")],
)
def test_status_warns_about_a_dangling_pin(home, make_repo, monkeypatch, capsys, pin, problem):
    add(home, "perso", "code")
    add(home, "pro", "work")
    git(home, "config", "--file", str(store.identity_path("pro")), "--unset", "user.email")
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "--local", resolve.PIN_KEY, pin)
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 0  # the folder identity applies, as the guard expects
    out = capsys.readouterr().out
    assert f'! this repo is pinned to "{pin}", {problem}' in out
    assert "  fix: git config --local --unset-all gitshift.identity" in out
    assert 'expected: "perso"' in out


def test_status_multi_valued_dangling_pin_has_a_fix_that_works(
    home, make_repo, monkeypatch, capsys
):
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "--local", "--add", resolve.PIN_KEY, "gone")
    git(repo, "config", "--local", "--add", resolve.PIN_KEY, "lost")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["status"]) == 0
    run_printed_fix(capsys.readouterr().out, repo)
    assert resolve.pinned_name(repo) is None
    assert main(["status"]) == 0
    assert "pinned" not in capsys.readouterr().out


def test_status_after_the_pinned_identity_is_removed(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code")
    add(home, "pro", "work")
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    assert main(["use", "pro", "--here"]) == 0
    assert main(["remove", "pro"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 1  # pro's email is still in the local config
    out = capsys.readouterr().out
    assert 'this repo is pinned to "pro", which does not exist' in out
    assert "overridden by this repo's local config (.git/config)" in out
    assert "  fix: git shift use perso --here" in out


# --- gh ----------------------------------------------------------------------------------


def account(login, active, valid=True, host="github.com"):
    if valid:
        header = f"✓ Logged in to {host} account {login} (keyring)"
    else:
        header = f"X Failed to log in to {host} account {login} (keyring)"
    return f"{host}\n  {header}\n  - Active account: {str(active).lower()}\n\n"


def status_with_gh(home, make_repo, monkeypatch, capsys, tmp_path, output):
    add(home, "perso", "code", "--gh", "mounimb")
    fake = write_fake_gh(tmp_path, output)
    monkeypatch.setattr(gh, "_executable", lambda: str(fake))
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    capsys.readouterr()
    assert main(["status"]) == 0  # gh problems never change the exit code
    return capsys.readouterr().out


def test_status_gh_active_account(home, make_repo, monkeypatch, capsys, tmp_path):
    output = account("alice", False) + account("MounimB", True)  # logins ignore case
    out = status_with_gh(home, make_repo, monkeypatch, capsys, tmp_path, output)
    assert "gh:       MounimB (active)" in out
    assert "fix" not in out


def test_status_gh_not_active_needs_no_cause_line(home, make_repo, monkeypatch, capsys, tmp_path):
    output = account("alice", True) + account("mounimb", False)
    out = status_with_gh(home, make_repo, monkeypatch, capsys, tmp_path, output)
    assert (
        "gh:       active account is alice, expected mounimb\n"
        "  fix: gh auth switch -h github.com -u mounimb\n"
    ) in out
    assert "!" not in out


@pytest.mark.parametrize(
    ("output", "active", "problem"),
    [
        (
            account("alice", True) + account("mounimb", False, valid=False),
            "alice",
            "! the gh login of mounimb is invalid or expired",
        ),
        (
            account("mounimb", True, valid=False),
            "none",
            "! the gh login of mounimb is invalid or expired",
        ),
        (account("alice", True), "alice", "! gh is not logged in as mounimb"),
        (
            account("alice", True) + account("mounimb", True, host="ghe.example.com"),
            "alice",
            "! gh is not logged in as mounimb",
        ),
        ("You are not logged into any GitHub hosts.\n", "none", "! gh is not logged in as mounimb"),
    ],
    ids=["invalid", "invalid-and-active", "unknown", "other-host", "no-account"],
)
def test_status_gh_problems(
    home, make_repo, monkeypatch, capsys, tmp_path, output, active, problem
):
    out = status_with_gh(home, make_repo, monkeypatch, capsys, tmp_path, output)
    assert f"gh:       active account is {active}, expected mounimb" in out
    assert problem in out
    assert "  fix: gh auth login -h github.com" in out


@pytest.mark.parametrize("variable", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_status_gh_token_variable(home, make_repo, monkeypatch, capsys, tmp_path, variable):
    # gh uses the token instead of its logins, and gh auth switch refuses to run
    monkeypatch.setenv(variable, "dummy-value")
    output = account("alice", True) + account("mounimb", False)
    out = status_with_gh(home, make_repo, monkeypatch, capsys, tmp_path, output)
    assert f"! {variable} is set, so gh uses that token instead of its logins" in out
    assert f"  fix: unset the {variable} environment variable\n" in out
    assert "gh auth switch" not in out
    assert "dummy-value" not in out  # presence only: the value is never shown


def test_status_gh_both_token_variables(home, make_repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "dummy-value")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-value")
    output = account("alice", True)
    out = status_with_gh(home, make_repo, monkeypatch, capsys, tmp_path, output)
    assert "! GH_TOKEN is set, so gh uses that token" in out  # GH_TOKEN wins in gh
    assert "  fix: unset the GH_TOKEN and GITHUB_TOKEN environment variables\n" in out


def test_status_without_gh_prints_no_gh_line(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code", "--gh", "mounimb")  # the home fixture hides gh
    monkeypatch.chdir(make_repo(home / "code" / "app"))
    capsys.readouterr()
    assert main(["status"]) == 0
    assert "gh:" not in capsys.readouterr().out
