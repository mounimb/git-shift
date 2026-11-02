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
    assert git(make_repo(home / "code" / "side"), "config", "user.email") == ("perso@example.com")


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


def test_remove_deletes_identity_and_rule(home, make_repo):
    add(home, folder="code")
    store.remove_identity("perso")
    assert store.list_identities() == []
    assert gitconfig.get("user.email", cwd=make_repo(home / "code" / "app")) is None


def test_remove_never_leaves_the_identities_folder(home):
    (home / ".gitconfig").write_text("[user]\n\tname = Original\n", encoding="utf-8")
    with pytest.raises(GitShiftError, match="invalid identity name"):
        store.remove_identity("../../../")
    assert (home / ".gitconfig").is_file()


def test_empty_author_name_rejected(home):
    with pytest.raises(GitShiftError, match="author name cannot be empty"):
        store.add_identity("perso", email="p@example.com", user_name=" ", folder=str(home))


def test_folder_with_glob_characters_matches_only_itself(home, make_repo):
    add(home, folder="code [old]")
    assert git(make_repo(home / "code [old]" / "app"), "config", "user.email") == (
        "perso@example.com"
    )
    assert gitconfig.get("user.email", cwd=make_repo(home / "code o" / "app")) is None


def test_add_warns_when_global_user_comes_after_the_include(home):
    (home / ".gitconfig").write_text(
        "[include]\n\tpath = ~/.gitconfig.local\n[user]\n\temail = global@example.com\n",
        encoding="utf-8",
    )
    _, warnings = add(home)
    assert any("overrides every identity" in w for w in warnings)


def test_identity_without_folder_applies_to_no_repo(home, make_repo):
    add(home, folder="code")
    (paths.identities_dir() / "broken.gitconfig").write_text(
        "[user]\n\temail = broken@example.com\n", encoding="utf-8"
    )
    store.write_rules(store.list_identities())
    assert gitconfig.get("user.email", cwd=make_repo(home / "elsewhere" / "app")) is None
