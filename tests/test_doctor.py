import re
import shlex
import sys

import pytest
from helpers import GH_STATUS_SAMPLE, git, make_git_refuse, write_fake_gh

from git_shift import doctor, gh, gitconfig, guard, paths, resolve, store
from git_shift.cli import main

OUT_OF_DATE = "rules.gitconfig is out of date"


def add(home, name="perso", folder="code", **kwargs):
    (home / folder).mkdir(parents=True, exist_ok=True)
    identity, _ = store.add_identity(
        name,
        email=f"{name}@example.com",
        user_name=name.title(),
        folder=str(home / folder),
        **kwargs,
    )
    return identity


def make_key(home, name="id_perso"):
    """A stand-in private key file: doctor only checks that it exists."""
    key = home / ".ssh" / name
    key.parent.mkdir(exist_ok=True)
    key.write_text("not a real key\n", encoding="utf-8")
    return key


def at(findings, level):
    return [finding for finding in findings if finding.level == level]


def errors(findings):
    return [finding.message for finding in at(findings, doctor.ERROR)]


def problems(findings):
    return [finding for finding in findings if finding.level != doctor.OK]


def only(findings, text):
    """The one finding whose message contains text."""
    found = [finding for finding in findings if text in finding.message]
    assert len(found) == 1, findings
    return found[0]


def none(findings, text):
    return not any(text in finding.message for finding in findings)


def run_fix(fix, cwd):
    """Run a printed fix as a shell would: a printed fix has to work."""
    program, *args = shlex.split(fix)
    assert program == "git"
    git(cwd, *args)  # raises if git fails


def rule_sections(rules):
    """rules.gitconfig as (header, [one [includeIf] section per rule])."""
    header, *sections = re.split(r"(?m)^(?=\[)", rules.read_text(encoding="utf-8"))
    return header, sections


# the whole run ----------------------------------------------------------------------------


def test_a_clean_setup_has_no_problems_and_exits_0(home, capsys):
    add(home)
    guard.install()
    assert problems(doctor.run(str(home))) == []
    capsys.readouterr()
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("✓ git ")
    assert all(line.startswith("✓ ") for line in out.splitlines()), out


def test_cli_prints_each_problem_with_its_fix_and_exits_1_on_an_error(home, capsys):
    key = paths.to_git_path(home / ".ssh" / "id_perso")
    add(home, ssh_key=key)
    capsys.readouterr()
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    missing = f'✗ the SSH key of "perso" is missing: {key}\n'
    assert f'{missing}  fix: ssh-keygen -t ed25519 -f "{key}"\n' in out
    assert (
        "! the guard is not installed: commits with the wrong identity are not blocked\n"
        "  fix: git shift guard install\n"
    ) in out


def test_cli_with_only_warnings_exits_0(home, capsys):
    add(home)  # no guard
    capsys.readouterr()
    assert main(["doctor"]) == 0
    assert "\n! the guard is not installed" in capsys.readouterr().out


def test_cli_indents_every_line_of_a_long_message(home, make_repo, monkeypatch, capsys):
    add(home)
    repo = make_repo(home / "code" / "app")
    make_git_refuse(repo)
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert (
        "\n✗ git refuses to work in this repository:\n"
        "  fatal: unknown repository extension found:\n  \tfoo\n"
    ) in out


# git --------------------------------------------------------------------------------------


def test_git_older_than_2_13_is_an_error(home, monkeypatch):
    add(home)
    monkeypatch.setattr(gitconfig, "git_version", lambda: (2, 12))
    finding = only(doctor.run(str(home)), "git 2.12")
    assert finding.level == doctor.ERROR
    assert "2.13" in finding.message


def test_git_version_is_reported(home):
    major, minor = gitconfig.git_version()
    assert only(doctor.run(str(home)), f"git {major}.{minor}").level == doctor.OK


# identities ------------------------------------------------------------------------------


def test_no_identities_is_a_warning_with_the_add_command(home):
    findings = doctor.run(str(home))
    finding = only(findings, "no identities yet")
    assert finding.level == doctor.WARN
    assert finding.fix.startswith("git shift add ")
    assert errors(findings) == []


def test_a_malformed_identity_is_an_error_and_the_other_checks_still_run(home):
    add(home, "perso", "code", ssh_key=str(home / ".ssh" / "id_perso"))
    add(home, "pro", "work")
    store.identity_path("pro").write_text("[user\n", encoding="utf-8")
    findings = doctor.run(str(home))
    finding = only(findings, 'cannot read identity "pro"')
    assert finding.level == doctor.ERROR
    assert "bad config line 1" in finding.message
    assert "pro.gitconfig" in finding.message  # the file git names
    assert only(findings, 'SSH key of "perso"').level == doctor.ERROR  # the good one counts
    assert not none(findings, "the guard is")


def test_a_malformed_repo_config_is_reported_with_the_file_git_names(
    home, make_repo, monkeypatch, capsys
):
    # every git config call from that repository fails, also the ones reading identities
    add(home)
    repo = make_repo(home / "code" / "app")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as config:
        config.write("[user\n")
    monkeypatch.chdir(repo)
    findings = doctor.run(str(repo))
    assert errors(findings)
    assert all(".git/config" in message for message in errors(findings))
    assert not none(findings, "the guard is")
    capsys.readouterr()
    assert main(["doctor"]) == 1


def test_an_identity_without_a_usable_folder_is_a_warning(home):
    add(home)
    path = store.identity_path("perso")
    git(home, "config", "--file", str(path), "gitshift.folder", "relative/dir")
    finding = only(doctor.run(str(home)), "no usable folder")
    assert finding.level == doctor.WARN
    no_folder = 'identity "perso" has no usable folder, so it applies to no repository'
    assert finding.message == no_folder
    assert finding.fix.startswith(f'git config --file "{paths.to_git_path(path)}" gitshift.folder ')


@pytest.mark.parametrize(
    ("key", "text"),
    [
        ("user.email", 'identity "perso" has no email, so it applies to no repository'),
        ("user.name", 'identity "perso" has no author name'),
    ],
)
def test_an_identity_without_an_email_or_name_is_a_warning(home, key, text):
    add(home)
    path = store.identity_path("perso")
    git(home, "config", "--file", str(path), key, "")
    finding = only(doctor.run(str(home)), text)
    assert finding.level == doctor.WARN
    assert finding.fix.startswith(f'git config --file "{paths.to_git_path(path)}" {key} ')


def test_a_folder_that_does_not_exist_yet_is_a_warning(home):
    store.add_identity("perso", email="perso@example.com", user_name="P", folder=str(home / "x"))
    finding = only(doctor.run(str(home)), "does not exist yet")
    assert finding.level == doctor.WARN
    assert paths.to_folder_rule(home / "x") in finding.message


def test_a_missing_ssh_key_is_an_error_with_the_keygen_fix(home):
    key = home / ".ssh" / "id_perso"
    add(home, ssh_key=str(key))
    finding = only(doctor.run(str(home)), "SSH key")
    assert finding.level == doctor.ERROR
    assert finding.fix == f'ssh-keygen -t ed25519 -f "{paths.to_git_path(key)}"'
    make_key(home)
    assert none(doctor.run(str(home)), "SSH key")


def test_a_nested_identity_inheriting_an_ssh_key_is_a_warning(home):
    key = make_key(home)
    add(home, "perso", "code", ssh_key=str(key))
    add(home, "pro", "code/pro")
    finding = only(doctor.run(str(home)), '"pro" is inside the folder of "perso"')
    assert finding.level == doctor.WARN
    assert paths.to_git_path(key) in finding.message


# the include of rules.gitconfig ------------------------------------------------------------


def test_a_missing_include_is_an_error_with_a_fix_that_works(home):
    add(home)
    gitconfig.unset("include.path", scope="global")
    finding = only(doctor.run(str(home)), "does not include rules.gitconfig")
    assert finding.level == doctor.ERROR
    rules = paths.to_git_path(paths.rules_file())
    assert finding.fix == f'git config --global --add include.path "{rules}"'
    run_fix(finding.fix, home)
    assert errors(doctor.run(str(home))) == []


def test_an_include_only_in_the_xdg_file_is_an_error(home):
    # git reads ~/.gitconfig after it, so that file can override every identity
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True)
    xdg.write_text("", encoding="utf-8")
    add(home)
    assert "rules.gitconfig" in xdg.read_text(encoding="utf-8")
    (home / ".gitconfig").write_text("[core]\n\teditor = vi\n", encoding="utf-8")
    finding = only(doctor.run(str(home)), "does not include rules.gitconfig")
    assert finding.level == doctor.ERROR
    assert finding.fix.startswith("git config --global --add include.path ")


def test_a_global_user_after_the_include_is_an_error(home):
    add(home)
    git(home, "config", "--global", "user.email", "global@example.com")
    finding = only(doctor.run(str(home)), "after the git-shift include")
    assert finding.level == doctor.ERROR
    assert finding.message == (
        "your global config sets user.email after the git-shift include, "
        "so it can override your identities"
    )
    assert "[include] or [includeIf]" in finding.fix


def test_an_include_after_the_git_shift_include_is_an_error(home):
    (home / ".gitconfig.work").write_text("[user]\n\temail = old@example.com\n", encoding="utf-8")
    add(home)
    with open(home / ".gitconfig", "a", encoding="utf-8") as config:
        config.write('[includeIf "gitdir/i:~/work/"]\n\tpath = .gitconfig.work\n')
    finding = only(doctor.run(str(home)), "after the git-shift include")
    assert finding.level == doctor.ERROR
    assert "user.email" in finding.message


def test_a_global_user_before_the_include_is_fine(home):
    git(home, "config", "--global", "user.email", "global@example.com")
    add(home)
    findings = doctor.run(str(home))
    assert errors(findings) == []
    assert only(findings, "includes rules.gitconfig").level == doctor.OK


# global author and committer overrides ------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "level"),
    [
        ("author.email", "author@example.com", doctor.ERROR),
        ("author.name", "Author", doctor.ERROR),
        ("committer.email", "committer@example.com", doctor.WARN),
    ],
)
def test_a_global_author_or_committer_override(home, key, value, level):
    add(home)
    git(home, "config", "--global", "--add", key, value)
    git(home, "config", "--global", "--add", key, value)  # several: --unset would fail
    finding = only(doctor.run(str(home)), f"sets {key}")
    assert finding.level == level
    assert finding.fix == f"git config --global --unset-all {key}"
    run_fix(finding.fix, home)
    assert none(doctor.run(str(home)), key)


def test_an_author_override_in_the_second_global_file_names_that_file(home):
    add(home)  # ~/.gitconfig exists: --global no longer writes the XDG file
    xdg = paths.xdg_git_config()
    xdg.parent.mkdir(parents=True)
    xdg.write_text("[author]\n\temail = xdg@example.com\n", encoding="utf-8")
    finding = only(doctor.run(str(home)), "sets author.email")
    assert finding.level == doctor.ERROR
    assert finding.fix == f'git config --file "{paths.to_git_path(xdg)}" --unset-all author.email'
    run_fix(finding.fix, home)
    assert none(doctor.run(str(home)), "author.email")


# rules.gitconfig is up to date ----------------------------------------------------------------


@pytest.mark.parametrize("edit", ["drop", "swap"])
def test_a_hand_edited_rules_file_is_out_of_date(home, edit):
    add(home, "perso", "code")
    add(home, "lab", "code/lab")  # deeper, so it must come last
    rules = paths.rules_file()
    header, sections = rule_sections(rules)
    assert len(sections) == 2
    edited = sections[:1] if edit == "drop" else sections[::-1]
    rules.write_text(header + "".join(edited), encoding="utf-8")
    finding = only(doctor.run(str(home)), OUT_OF_DATE)
    assert finding.level == doctor.ERROR
    assert finding.fix == "git shift doctor --fix"


def test_a_missing_rules_file_is_out_of_date(home):
    add(home)
    paths.rules_file().unlink()
    assert only(doctor.run(str(home)), OUT_OF_DATE).level == doctor.ERROR


def test_an_entry_added_to_the_rules_file_by_hand_is_out_of_date(home):
    add(home)
    git(home, "config", "--file", str(paths.rules_file()), "user.email", "x@example.com")
    assert only(doctor.run(str(home)), OUT_OF_DATE).level == doctor.ERROR


def test_rules_that_differ_only_in_ascii_case_are_up_to_date(home):
    # gitdir/i ignores A-Z case, as git does: the rule still matches
    add(home)
    rules = paths.rules_file()
    rules.write_text(rules.read_text(encoding="utf-8").upper(), encoding="utf-8")
    assert none(doctor.run(str(home)), OUT_OF_DATE)


def test_a_hand_edited_folder_makes_the_rules_out_of_date_until_fix(home, make_repo, capsys):
    add(home)
    (home / "projects").mkdir()
    path = store.identity_path("perso")
    git(home, "config", "--file", str(path), "gitshift.folder", str(home / "projects"))
    repo = make_repo(home / "projects" / "app")
    assert gitconfig.get("user.email", cwd=repo) is None  # git still has the old rule
    assert only(doctor.run(str(home)), OUT_OF_DATE).level == doctor.ERROR
    capsys.readouterr()
    assert main(["doctor", "--fix"]) == 0
    assert git(repo, "config", "user.email") == "perso@example.com"


def test_a_folder_that_resolves_differently_after_add_is_out_of_date(home):
    # added before the folder existed; the folder created then has another spelling that
    # the file system takes for the same one (Windows, macOS), but gitdir/i does not
    store.add_identity("perso", email="perso@example.com", user_name="P", folder=str(home / "É"))
    (home / "é").mkdir()
    if not (home / "É").exists():
        pytest.skip("case-sensitive file system: É and é are two folders")
    assert only(doctor.run(str(home)), OUT_OF_DATE).level == doctor.ERROR


def test_up_to_date_rules_are_ok(home):
    add(home)
    assert only(doctor.run(str(home)), "rules.gitconfig matches").level == doctor.OK


# doctor --fix ---------------------------------------------------------------------------------


@pytest.mark.parametrize("edit", ["drop", "swap"])
def test_fix_rewrites_a_hand_edited_rules_file_and_nothing_else(home, capsys, edit):
    add(home, "perso", "code")
    add(home, "lab", "code/lab")
    guard.install()
    rules = paths.rules_file()
    written = rules.read_bytes()
    header, sections = rule_sections(rules)
    edited = sections[:1] if edit == "drop" else sections[::-1]
    rules.write_text(header + "".join(edited), encoding="utf-8")
    global_config = (home / ".gitconfig").read_bytes()
    capsys.readouterr()
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert f"✗ {OUT_OF_DATE}" in out
    assert "  fix: git shift doctor --fix\n" in out

    assert main(["doctor", "--fix"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("✓ rewrote rules.gitconfig from your 2 identities\n")
    assert OUT_OF_DATE not in out
    assert rules.read_bytes() == written
    assert (home / ".gitconfig").read_bytes() == global_config
    assert main(["doctor"]) == 0


def test_fix_with_up_to_date_rules_rewrites_nothing_and_says_what_it_leaves(
    home, monkeypatch, capsys
):
    add(home)  # the guard is not installed: a warning --fix leaves alone
    calls = []
    monkeypatch.setattr(store, "write_rules", calls.append)
    capsys.readouterr()
    assert main(["doctor", "--fix"]) == 0
    out = capsys.readouterr().out
    assert calls == []
    assert out.startswith("✓ rules.gitconfig already matches your 1 identity: nothing to rewrite\n")
    assert "! the guard is not installed" in out
    assert out.endswith("--fix only rewrites rules.gitconfig: run the other fixes yourself\n")


def test_fix_leaves_the_rules_alone_when_an_identity_cannot_be_read(home, capsys):
    add(home, "perso", "code")
    add(home, "pro", "work")
    rules = paths.rules_file()
    before = rules.read_bytes()
    store.identity_path("pro").write_text("[user\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["doctor", "--fix"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("✗ rules.gitconfig not rewritten: an identity file cannot be read\n")
    assert '✗ cannot read identity "pro"' in out
    assert rules.read_bytes() == before


def test_fix_never_writes_the_global_config(home, capsys):
    add(home)
    gitconfig.unset("include.path", scope="global")
    paths.rules_file().unlink()
    global_config = (home / ".gitconfig").read_bytes()
    capsys.readouterr()
    assert main(["doctor", "--fix"]) == 1  # the include is still missing
    out = capsys.readouterr().out
    assert out.startswith("✓ rewrote rules.gitconfig from your 1 identity\n")
    assert "✗ " in out and "does not include rules.gitconfig" in out
    assert (home / ".gitconfig").read_bytes() == global_config


# gh -------------------------------------------------------------------------------------------


def account(login, valid=True, host="github.com", active=False):
    if valid:
        header = f"✓ Logged in to {host} account {login} (keyring)"
    else:
        header = f"X Failed to log in to {host} account {login} (keyring)"
    return f"{host}\n  {header}\n  - Active account: {str(active).lower()}\n\n"


def fake_gh(monkeypatch, tmp_path, output):
    fake = write_fake_gh(tmp_path, output)
    monkeypatch.setattr(gh, "_executable", lambda: str(fake))


def test_without_gh_the_github_checks_are_skipped(home):
    add(home, gh="alice")  # the home fixture hides gh
    finding = only(doctor.run(str(home)), "gh not installed or not responding")
    assert finding.level == doctor.OK


def test_valid_gh_logins_are_ok(home, monkeypatch, tmp_path):
    fake_gh(monkeypatch, tmp_path, account("alice", active=True) + account("Bob"))
    add(home, gh="bob")  # logins ignore case
    findings = doctor.run(str(home))
    assert [f for f in problems(findings) if "the guard is" not in f.message] == []
    assert only(findings, "gh logins").level == doctor.OK


def test_an_invalid_gh_login_is_a_warning_with_two_fixes(home, monkeypatch, tmp_path):
    fake_gh(monkeypatch, tmp_path, GH_STATUS_SAMPLE)  # carol's login is invalid
    add(home)
    findings = doctor.run(str(home))
    finding = only(findings, "carol")
    assert finding.level == doctor.WARN
    assert finding.message == "the gh login of carol on github.com is invalid or expired"
    assert finding.fix == (
        "gh auth login -h github.com   (or forget it: gh auth logout -h github.com -u carol)"
    )
    assert errors(findings) == []


def test_an_identity_whose_gh_login_is_invalid_is_a_warning(home, monkeypatch, tmp_path):
    fake_gh(monkeypatch, tmp_path, GH_STATUS_SAMPLE)
    add(home, gh="carol")
    findings = doctor.run(str(home))
    finding = only(findings, "carol")  # once, with the identity that needs it
    assert finding.level == doctor.WARN
    assert finding.message == (
        'the gh login of carol is invalid or expired, which identity "perso" expects'
    )
    assert finding.fix == "gh auth login -h github.com"
    assert errors(findings) == []


def test_an_identity_whose_gh_login_is_missing_is_a_warning(home, monkeypatch, tmp_path):
    fake_gh(monkeypatch, tmp_path, GH_STATUS_SAMPLE)
    add(home, gh="dave")
    finding = only(doctor.run(str(home)), "dave")
    assert finding.level == doctor.WARN
    assert finding.message == 'gh is not logged in as dave, which identity "perso" expects'
    assert finding.fix == "gh auth login -h github.com"


def test_every_login_invalid_on_a_host_is_one_offline_warning(home, monkeypatch, tmp_path):
    output = account("carol", valid=False) + account("dave", valid=False, active=True)
    fake_gh(monkeypatch, tmp_path, output)
    add(home, gh="carol")
    findings = doctor.run(str(home))
    finding = only(findings, "could not verify")
    assert finding.level == doctor.WARN
    assert finding.message.startswith("could not verify the gh logins on github.com (offline?)")
    assert none(findings, "carol") and none(findings, "dave")


@pytest.mark.parametrize("variable", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_a_gh_token_variable_is_a_warning_without_its_value(home, monkeypatch, tmp_path, variable):
    monkeypatch.setenv(variable, "dummy-value")
    fake_gh(monkeypatch, tmp_path, GH_STATUS_SAMPLE)
    add(home, gh="alice")
    findings = doctor.run(str(home))
    finding = only(findings, f"{variable} is set")
    assert finding.level == doctor.WARN
    assert finding.message == f"{variable} is set, so gh uses that token instead of its logins"
    assert finding.fix == f"unset the {variable} environment variable"
    assert "dummy-value" not in repr(findings)


def test_a_gh_token_variable_matters_only_to_identities_with_a_gh_login(
    home, monkeypatch, tmp_path
):
    monkeypatch.setenv("GH_TOKEN", "dummy-value")
    fake_gh(monkeypatch, tmp_path, account("alice"))
    add(home)
    assert none(doctor.run(str(home)), "GH_TOKEN")


# the guard ------------------------------------------------------------------------------------


def test_the_guard_not_installed_is_a_warning(home):
    add(home)
    finding = only(doctor.run(str(home)), "the guard is not installed")
    assert finding.level == doctor.WARN
    assert finding.fix == "git shift guard install"


def test_the_guard_installed_is_ok(home):
    add(home)
    guard.install()
    assert only(doctor.run(str(home)), "the guard is installed").level == doctor.OK


def test_the_guard_overridden_by_a_later_include_is_an_error(home, tmp_path):
    add(home)
    guard.install()
    other = paths.to_git_path(tmp_path / "other")
    (home / "extra.gitconfig").write_text(f"[core]\n\thooksPath = {other}\n", encoding="utf-8")
    with open(home / ".gitconfig", "a", encoding="utf-8") as config:
        config.write("[include]\n\tpath = extra.gitconfig\n")
    finding = only(doctor.run(str(home)), "not active")
    assert finding.level == doctor.ERROR
    assert other in finding.message
    assert "extra.gitconfig" in finding.fix


def test_the_guard_with_a_missing_python_is_an_error(home, tmp_path, monkeypatch):
    add(home)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "gone" / "python"))
    guard.install()
    finding = only(doctor.run(str(home)), "Python")
    assert finding.level == doctor.ERROR
    assert finding.fix == "git shift guard install"


def test_the_guard_with_its_hooks_deleted_is_an_error(home):
    add(home)
    guard.install()
    (paths.hooks_dir() / "pre-commit").unlink()
    finding = only(doctor.run(str(home)), "guard's hooks are missing")
    assert finding.level == doctor.ERROR
    assert finding.fix == "git shift guard install"


def test_a_hooks_path_in_an_include_if_only_turns_the_guard_off_where_it_applies(
    home, make_repo, tmp_path, monkeypatch
):
    add(home)
    guard.install()
    work_hooks = paths.to_git_path(tmp_path / "work hooks")
    (home / "work.gitconfig").write_text(f"[core]\n\thooksPath = {work_hooks}\n", encoding="utf-8")
    with open(home / ".gitconfig", "a", encoding="utf-8") as config:
        config.write(f'[includeIf "gitdir:{paths.to_git_path(home / "work")}/"]\n')
        config.write("\tpath = work.gitconfig\n")
    repo = make_repo(home / "work" / "app")
    monkeypatch.chdir(repo)
    findings = doctor.run(str(repo))
    assert none(findings, "not active")  # the guard runs everywhere else
    finding = only(findings, "the guard does not run in this repository")
    assert finding.level == doctor.WARN
    assert finding.message.endswith(
        f"core.hooksPath is set in {paths.to_git_path(home / 'work.gitconfig')}"
    )
    assert none(doctor.run(str(home)), "does not run")


# the current repository -------------------------------------------------------------------------


def test_a_repository_where_git_refuses_to_work_is_an_error(home, make_repo, monkeypatch):
    add(home)
    repo = make_repo(home / "code" / "app")
    make_git_refuse(repo)
    monkeypatch.chdir(repo)
    findings = doctor.run(str(repo))
    finding = only(findings, "unknown repository extension")
    assert finding.level == doctor.ERROR
    assert not none(findings, "the guard is")  # the other checks still run


def test_a_repository_with_the_expected_email_is_ok(home, make_repo):
    add(home)
    guard.install()
    repo = make_repo(home / "code" / "app")
    findings = doctor.run(str(repo))
    assert problems(findings) == []
    finding = only(findings, "this repo commits as perso@example.com")
    assert finding.level == doctor.OK
    assert '"perso"' in finding.message


def test_outside_a_repository_there_is_no_repository_check(home):
    add(home)
    assert none(doctor.run(str(home)), "this repo")


@pytest.mark.parametrize(
    ("pin", "problem"),
    [("gone", "which does not exist"), ("pro", "which has no email, so the pin is ignored")],
)
def test_a_dangling_pin_is_worded_like_status(home, make_repo, pin, problem):
    add(home, "perso", "code")
    add(home, "pro", "work")
    git(home, "config", "--file", str(store.identity_path("pro")), "--unset", "user.email")
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "--local", resolve.PIN_KEY, pin)
    finding = only(doctor.run(str(repo)), "pinned")
    assert finding.level == doctor.WARN
    assert finding.message == f'this repo is pinned to "{pin}", {problem}'
    assert finding.fix == "git config --local --unset-all gitshift.identity"


def test_a_pinned_repository_expects_its_pin(home, make_repo, monkeypatch, capsys):
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    monkeypatch.chdir(repo)
    assert main(["use", "pro", "--here"]) == 0
    findings = doctor.run(str(repo))
    assert errors(findings) == []
    assert '"pro"' in only(findings, "this repo commits as").message


def test_a_local_email_override_is_explained_like_status(home, make_repo):
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "user.email", "x@example.com")
    finding = only(doctor.run(str(repo)), "local config")
    assert finding.level == doctor.ERROR
    assert finding.message == (
        'this repo commits as x@example.com, not perso@example.com ("perso"): '
        "overridden by this repo's local config (.git/config)"
    )
    assert finding.fix == "git shift use perso --here"


def test_a_global_email_beating_a_folder_rule_does_not_say_run_doctor(home, make_repo):
    add(home)
    git(home, "config", "--global", "user.email", "global@example.com")  # after the include
    findings = doctor.run(str(make_repo(home / "code" / "app")))
    finding = only(findings, "this repo commits as")
    assert finding.level == doctor.ERROR
    assert finding.message.endswith(": your global git config overrides git-shift rules")
    assert only(findings, "after the git-shift include").level == doctor.ERROR  # the cause


def test_a_local_author_email_has_a_fix_that_works(home, make_repo):
    add(home)
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "--local", "author.email", "author@example.com")
    finding = only(doctor.run(str(repo)), "author.email")
    assert finding.level == doctor.ERROR
    assert finding.message.endswith(
        ': author.email in this repo\'s .git/config replaces the email of "perso"'
    )
    assert finding.fix == "git config --local --unset-all author.email"
    run_fix(finding.fix, repo)
    assert errors(doctor.run(str(repo))) == []


def test_a_worktree_is_checked_against_its_main_clone(home, make_repo):
    add(home, "perso", "code")
    add(home, "pro", "work")
    repo = make_repo(home / "code" / "app")
    git(repo, "commit", "--allow-empty", "-q", "-m", "init")
    worktree = home / "work" / "wt"
    git(repo, "worktree", "add", "-q", str(worktree))
    findings = doctor.run(str(worktree))
    assert errors(findings) == []
    assert '"perso"' in only(findings, "this repo commits as").message


@pytest.mark.parametrize(
    ("url", "warned"),
    [
        ("git@github.com:alice/app.git", True),
        ("ssh://git@github.com/alice/app.git", True),
        ("https://github.com/alice/app.git", False),
        (None, False),
    ],
)
def test_a_keyed_identity_without_its_key_is_a_warning_only_over_ssh(home, make_repo, url, warned):
    add(home, ssh_key=str(make_key(home)))
    repo = make_repo(home / "code" / "app", remote=url)
    git(repo, "config", "--local", "core.sshCommand", "ssh -i other")
    findings = doctor.run(str(repo))
    if not warned:
        assert none(findings, "for SSH")
        return
    finding = only(findings, "for SSH")
    assert finding.level == doctor.WARN
    assert finding.message.startswith('this repo still uses ssh -i other for SSH, but "perso"')
    assert finding.fix == "git shift use perso --here"


def test_the_ssh_fix_depends_on_where_the_command_comes_from(home, make_repo, monkeypatch):
    add(home, ssh_key=str(make_key(home)))
    repo = make_repo(home / "code" / "app", remote="git@github.com:alice/app.git")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "core.sshCommand", "ssh -i other")
    assert only(doctor.run(str(repo)), "for SSH").fix == (
        "git config --worktree --unset-all core.sshCommand"
    )
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i other")
    assert only(doctor.run(str(repo)), "for SSH").fix == (
        "unset the GIT_SSH_COMMAND environment variable"
    )


def test_a_keyed_identity_using_its_key_gives_no_ssh_warning(home, make_repo):
    add(home, ssh_key=str(make_key(home)))
    repo = make_repo(home / "code" / "app", remote="git@github.com:alice/app.git")
    assert none(doctor.run(str(repo)), "for SSH")


def test_a_local_hooks_path_turns_the_guard_off_in_this_repository(home, make_repo):
    add(home)
    guard.install()
    repo = make_repo(home / "code" / "app")
    git(repo, "config", "core.hooksPath", ".husky")
    finding = only(doctor.run(str(repo)), "the guard does not run in this repository")
    assert finding.level == doctor.WARN
    assert finding.message.endswith(".git/config")
