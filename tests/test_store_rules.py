import os
import stat

import pytest
from helpers import git

from git_shift import gitconfig, paths, store
from git_shift.errors import GitShiftError


def add(home, name="perso", folder="code", **kwargs):
    identity, warnings = store.add_identity(
        name,
        email=f"{name}@example.com",
        user_name=name.title(),
        folder=str(home / folder),
        **kwargs,
    )
    return identity, warnings


def test_add_creates_identity_rules_and_one_global_include(home):
    identity, warnings = add(home)
    assert identity.path.is_file()
    rules = paths.rules_file().read_text(encoding="utf-8")
    assert f"gitdir/i:{identity.folder}" in rules
    assert gitconfig.get_all("include.path", scope="global") == [
        paths.to_git_path(paths.rules_file())
    ]
    assert any("does not exist yet" in w for w in warnings)


def test_commit_in_identity_folder_uses_identity_email(home, make_repo):
    add(home, folder="code")
    repo = make_repo(home / "code" / "app")
    git(repo, "commit", "--allow-empty", "-q", "-m", "init")
    assert git(repo, "log", "-1", "--format=%ae") == "perso@example.com"


def test_deeper_folder_wins(home, make_repo):
    add(home, "pro", "code/pro")
    add(home, "perso", "code")
    assert git(make_repo(home / "code" / "pro" / "app"), "config", "user.email") == (
        "pro@example.com"
    )
    assert git(make_repo(home / "code" / "side"), "config", "user.email") == "perso@example.com"


def test_global_include_added_only_once(home):
    add(home, "a", "a")
    add(home, "b", "b")
    assert len(gitconfig.get_all("include.path", scope="global")) == 1


def test_duplicate_name_or_folder_rejected(home):
    add(home, "a", "a")
    with pytest.raises(GitShiftError, match="already exists"):
        add(home, "a", "other")
    with pytest.raises(GitShiftError, match="already used"):
        add(home, "b", "a")


def test_duplicate_folder_check_folds_case_like_git(home):
    # to git these are two rules, so both identities can apply; only A-Z is folded
    add(home, "a", "straße")
    add(home, "b", "STRASSE")
    with pytest.raises(GitShiftError, match='already used by "b"'):
        add(home, "c", "strasse")


def test_missing_ssh_key_warns(home):
    _, warnings = add(home, ssh_key=str(home / ".ssh" / "nope"))
    assert any("SSH key" in w for w in warnings)


def test_global_config_backed_up_before_first_change(home):
    (home / ".gitconfig").write_text("[user]\n\tname = Original\n", encoding="utf-8")
    add(home, "a", "a")
    add(home, "b", "b")
    backups = list(paths.backups_dir().iterdir())
    assert len(backups) == 1
    assert "Original" in backups[0].read_text(encoding="utf-8")
    assert "rules.gitconfig" not in backups[0].read_text(encoding="utf-8")


def test_remove_deletes_identity_and_rule(home, make_repo):
    add(home, folder="code")
    store.remove_identity("perso")
    assert store.list_identities() == []
    assert "gitdir" not in paths.rules_file().read_text(encoding="utf-8")
    assert gitconfig.get("user.email", cwd=make_repo(home / "code" / "app")) is None


def test_remove_never_leaves_the_identities_folder(home):
    (home / ".gitconfig").write_text("[user]\n\tname = Original\n", encoding="utf-8")
    with pytest.raises(GitShiftError, match="invalid identity name"):
        store.remove_identity("../../../")
    assert (home / ".gitconfig").is_file()


def test_remove_with_a_malformed_sibling_changes_nothing(home):
    add(home, folder="code")
    (paths.identities_dir() / "bad.gitconfig").write_text("[user\n", encoding="utf-8")
    with pytest.raises(GitShiftError):
        store.remove_identity("perso")
    assert store.identity_path("perso").is_file()


def test_empty_author_name_rejected(home):
    with pytest.raises(GitShiftError, match="author name cannot be empty"):
        store.add_identity("perso", email="p@example.com", user_name=" ", folder=str(home))


def test_folder_with_glob_characters_matches_only_itself(home, make_repo):
    add(home, folder="code [old]")
    assert git(make_repo(home / "code [old]" / "app"), "config", "user.email") == (
        "perso@example.com"
    )
    assert gitconfig.get("user.email", cwd=make_repo(home / "code o" / "app")) is None


INCLUDE_THEN_USER = "[include]\n\tpath = ~/.gitconfig.local\n[user]\n\temail = global@example.com\n"


def rules_section():
    rules = paths.to_git_path(paths.rules_file())
    return f'[include]\n\tpath = "{rules}"\n'


def test_add_puts_the_include_after_a_user_section_below_an_existing_include(home, make_repo):
    # git config --add would put it in the top [include], before [user]: the global email
    # would then beat every identity
    (home / ".gitconfig").write_text(INCLUDE_THEN_USER, encoding="utf-8")
    _, warnings = add(home)
    assert not any("override" in w for w in warnings)
    assert (home / ".gitconfig").read_text(encoding="utf-8") == INCLUDE_THEN_USER + rules_section()
    assert store.keys_after_global_include() == []
    assert git(make_repo(home / "code" / "app"), "config", "user.email") == "perso@example.com"
    assert len(list(paths.backups_dir().glob("gitconfig-*"))) == 1


def test_the_appended_include_is_written_once(home):
    (home / ".gitconfig").write_text(INCLUDE_THEN_USER, encoding="utf-8")
    add(home, "perso", "code")
    after_first = (home / ".gitconfig").read_bytes()
    add(home, "pro", "work")
    assert (home / ".gitconfig").read_bytes() == after_first


def test_the_include_is_appended_after_a_last_line_without_newline(home, make_repo):
    config = INCLUDE_THEN_USER.rstrip("\n")
    (home / ".gitconfig").write_text(config, encoding="utf-8", newline="\n")
    add(home)
    text = (home / ".gitconfig").read_text(encoding="utf-8")
    assert text == config + "\n" + rules_section()
    assert git(make_repo(home / "code" / "app"), "config", "user.email") == "perso@example.com"


def test_the_appended_include_quotes_a_path_git_would_misread(home, monkeypatch, make_repo):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "my cfg #1; x"))  # a comment, unquoted
    (home / ".gitconfig").write_text(INCLUDE_THEN_USER, encoding="utf-8")
    add(home)
    rules = paths.to_git_path(paths.rules_file())
    assert gitconfig.get_all("include.path", scope="global")[-1] == rules
    assert git(make_repo(home / "code" / "app"), "config", "user.email") == "perso@example.com"


def test_the_appended_include_respects_git_s_lock(home):
    (home / ".gitconfig").write_text(INCLUDE_THEN_USER, encoding="utf-8")
    lock = home / ".gitconfig.lock"
    lock.write_text("", encoding="utf-8")  # git is writing it, or crashed meanwhile
    with pytest.raises(GitShiftError, match="lock") as error:
        add(home)
    assert f"if no git command is running, delete {paths.to_git_path(lock)}" in str(error.value)
    assert (home / ".gitconfig").read_text(encoding="utf-8") == INCLUDE_THEN_USER
    assert store.list_identities() == []


def test_an_append_git_would_misread_leaves_the_file_untouched(home):
    # a last line ending with \ continues on the next one: git would read
    # user.name = "Some [include]" and a stray user.path
    config = INCLUDE_THEN_USER + "\tname = Some \\"
    (home / ".gitconfig").write_text(config, encoding="utf-8", newline="\n")
    before = (home / ".gitconfig").read_bytes()
    with pytest.raises(GitShiftError) as error:
        add(home)
    assert f"did not change {paths.to_git_path(home / '.gitconfig')}: " in str(error.value)
    assert (home / ".gitconfig").read_bytes() == before
    assert not (home / ".gitconfig.lock").exists()
    assert store.list_identities() == []
    assert gitconfig.get("user.name", scope="global") == "Some "


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_a_symlinked_global_config_stays_a_symlink(home, make_repo):
    dotfiles = home / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "gitconfig"
    real.write_text(INCLUDE_THEN_USER, encoding="utf-8")
    real.chmod(0o600)
    (home / ".gitconfig").symlink_to(real)
    add(home)
    assert (home / ".gitconfig").is_symlink()
    assert real.read_text(encoding="utf-8") == INCLUDE_THEN_USER + rules_section()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert git(make_repo(home / "code" / "app"), "config", "user.email") == "perso@example.com"


def test_an_include_section_at_the_end_takes_the_path_through_git(home):
    (home / ".gitconfig").write_text(
        "[user]\n\temail = global@example.com\n[include]\n\tpath = ~/.gitconfig.local\n",
        encoding="utf-8",
    )
    add(home)
    text = (home / ".gitconfig").read_text(encoding="utf-8")
    assert text.count("[include]") == 1
    assert store.keys_after_global_include() == []


def test_add_warns_when_global_user_comes_after_the_include(home):
    add(home, "perso", "code")
    git(home, "config", "--global", "user.email", "global@example.com")  # a [user] at the end
    _, warnings = add(home, "pro", "work")
    assert any("can override your identities" in w for w in warnings)


def test_identity_without_folder_applies_to_no_repo(home, make_repo):
    add(home, folder="code")
    (paths.identities_dir() / "broken.gitconfig").write_text(
        "[user]\n\temail = broken@example.com\n", encoding="utf-8"
    )
    store.write_rules(store.list_identities())
    assert gitconfig.get("user.email", cwd=make_repo(home / "elsewhere" / "app")) is None


def test_failed_global_include_leaves_nothing_behind(home):
    (home / ".gitconfig").write_text("[user]\n\tname = Original\n", encoding="utf-8")
    (home / ".gitconfig.lock").write_text("", encoding="utf-8")  # git cannot lock the file
    with pytest.raises(GitShiftError):
        add(home)
    assert store.list_identities() == []
    (home / ".gitconfig.lock").unlink()
    add(home)
    assert [i.name for i in store.list_identities()] == ["perso"]


def test_failed_rules_write_removes_the_new_identity(home, monkeypatch):
    def broken(identities):
        raise GitShiftError("rules write failed")

    monkeypatch.setattr(store, "write_rules", broken)
    with pytest.raises(GitShiftError, match="rules write failed"):
        add(home)
    assert not store.identity_path("perso").exists()


def test_nested_identity_without_ssh_key_warns_about_the_inherited_key(home):
    add(home, "perso", "code", ssh_key=str(home / ".ssh" / "id_perso"))
    _, warnings = add(home, "pro", "code/pro")
    assert any('"pro"' in w and "id_perso" in w for w in warnings)


def test_outer_identity_added_after_the_inner_one_warns_too(home):
    add(home, "pro", "code/pro")
    _, warnings = add(home, "perso", "code", ssh_key=str(home / ".ssh" / "id_perso"))
    assert any('"pro"' in w and "id_perso" in w for w in warnings)


def test_three_levels_name_the_deepest_ssh_key(home):
    add(home, "perso", "code", ssh_key=str(home / ".ssh" / "id_perso"))
    add(home, "pro", "code/pro", ssh_key=str(home / ".ssh" / "id_pro"))
    _, warnings = add(home, "lab", "code/pro/lab")
    inherited = [w for w in warnings if '"lab"' in w]
    assert len(inherited) == 1 and "id_pro" in inherited[0]


def test_inherited_ssh_keys_lists_every_pair_with_the_deepest_key(home):
    add(home, "perso", "code", ssh_key=str(home / ".ssh" / "id_perso"))
    add(home, "pro", "code/pro", ssh_key=str(home / ".ssh" / "id_pro"))
    add(home, "lab", "code/pro/lab")
    add(home, "docs", "code/docs")
    add(home, "work", "work")
    pairs = store.inherited_ssh_keys(store.list_identities())
    assert [(inner.name, outer.name) for inner, outer in pairs] == [
        ("docs", "perso"),
        ("lab", "pro"),
    ]


def append_to_global_config(home, text):
    with open(home / ".gitconfig", "a", encoding="utf-8") as config:
        config.write(text)


def test_add_warns_about_a_user_include_if_after_the_git_shift_include(home):
    (home / ".gitconfig.work").write_text("[user]\n\temail = old@example.com\n", encoding="utf-8")
    add(home, "perso", "code")
    append_to_global_config(home, '[includeIf "gitdir/i:~/work/"]\n\tpath = .gitconfig.work\n')
    _, warnings = add(home, "pro", "other")
    assert any("user.email" in w and "can override" in w for w in warnings)


def test_unreadable_include_after_git_shift_does_not_fail_add(home):
    (home / ".gitconfig-old").write_text("[user\n", encoding="utf-8")
    add(home, "perso", "code")
    append_to_global_config(home, '[includeIf "gitdir/i:~/old/"]\n\tpath = ~/.gitconfig-old\n')
    identity, _ = add(home, "pro", "work")
    assert identity.path.is_file()


def test_relative_include_of_the_rules_file_is_recognised(home, monkeypatch):
    (home / ".gitconfig").write_text(
        "[include]\n\tpath = .config/git-shift/rules.gitconfig\n", encoding="utf-8"
    )
    monkeypatch.chdir(home.parent)  # git resolves it against ~/.gitconfig, not the cwd
    add(home)
    assert len(gitconfig.get_all("include.path", scope="global")) == 1


def test_include_goes_to_git_config_global_when_set(home, monkeypatch):
    target = home / "custom.gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(target))
    add(home)
    assert paths.to_git_path(paths.rules_file()) in target.read_text(encoding="utf-8")
    assert not (home / ".gitconfig").exists()


def test_empty_folder_rejected(home):
    with pytest.raises(GitShiftError, match="folder cannot be empty"):
        store.add_identity("perso", email="p@example.com", user_name="P", folder=" ")


def test_stray_file_in_backups_does_not_skip_the_backup(home):
    (home / ".gitconfig").write_text("[user]\n\tname = Original\n", encoding="utf-8")
    paths.backups_dir().mkdir(parents=True)
    (paths.backups_dir() / ".DS_Store").write_text("", encoding="utf-8")
    add(home)
    assert len(list(paths.backups_dir().glob("gitconfig-*"))) == 1


def test_remove_works_on_a_hand_broken_identity(home):
    add(home, folder="code")
    store.identity_path("perso").write_text("[user\n", encoding="utf-8")
    store.remove_identity("perso")
    assert not store.identity_path("perso").exists()
    assert "gitdir" not in paths.rules_file().read_text(encoding="utf-8")


def test_add_warns_about_a_global_author_email(home):
    (home / ".gitconfig").write_text("[author]\n\temail = author@example.com\n", encoding="utf-8")
    _, warnings = add(home)
    assert any("author.email" in w for w in warnings)


def test_default_layout_gives_no_override_warning(home):
    (home / ".gitconfig").write_text("[user]\n\temail = global@example.com\n", encoding="utf-8")
    _, warnings = add(home)
    assert not any("override" in w or "instead of" in w for w in warnings)


def test_inherited_ssh_key_warning_matches_git(home, make_repo):
    add(home, "perso", "code", ssh_key=str(home / ".ssh" / "id_perso"))
    add(home, "pro", "code/pro")
    inner = make_repo(home / "code" / "pro" / "app")
    assert "id_perso" in git(inner, "config", "core.sshCommand")


def test_include_goes_to_the_xdg_config_when_it_is_the_only_one(home):
    xdg = home / ".config" / "git" / "config"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("[user]\n\tname = Original\n", encoding="utf-8")
    add(home)
    assert "rules.gitconfig" in xdg.read_text(encoding="utf-8")
    assert not (home / ".gitconfig").exists()


@pytest.mark.parametrize("login", ["-x", "https://github.com/x", "a b"])
def test_invalid_github_login_rejected(home, login):
    with pytest.raises(GitShiftError, match="invalid GitHub login"):
        add(home, gh=login)


def test_github_login_loses_a_leading_at(home):
    identity, _ = add(home, gh="@mounimb")
    assert identity.gh == "mounimb"
