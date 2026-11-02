import pytest

from git_shift import paths, store
from git_shift.errors import GitShiftError


def make_identity(home, name="perso", ssh_key=None, gh=None):
    return store.Identity(
        name=name,
        email=f"{name}@example.com",
        user_name="Test User",
        folder=paths.to_folder_rule(home / "code" / name),
        ssh_key=ssh_key,
        gh=gh,
    )


def test_save_and_load_roundtrip(home):
    identity = make_identity(home, ssh_key=paths.to_git_path(home / ".ssh" / "id"), gh="mounimb")
    store.save(identity)
    assert store.load("perso") == identity


def test_identity_file_is_plain_git_config(home):
    identity = make_identity(home, ssh_key=paths.to_git_path(home / ".ssh" / "id"))
    store.save(identity)
    text = identity.path.read_text(encoding="utf-8")
    assert "email = perso@example.com" in text
    assert "sshCommand = ssh -i" in text
    assert "IdentitiesOnly=yes" in text


def test_list_identities_sorted_by_name(home):
    store.save(make_identity(home, "pro"))
    store.save(make_identity(home, "perso"))
    assert [i.name for i in store.list_identities()] == ["perso", "pro"]


def test_list_identities_empty(home):
    assert store.list_identities() == []


def test_load_unknown_identity_fails(home):
    with pytest.raises(GitShiftError, match="no identity named"):
        store.load("nope")


def test_load_rejects_names_that_leave_the_identities_folder(home):
    # identities/../../../.gitconfig is ~/.gitconfig: remove would delete it
    (home / ".gitconfig").write_text("[user]\n\tname = Original\n", encoding="utf-8")
    with pytest.raises(GitShiftError, match="invalid identity name"):
        store.load("../../../")


def test_list_identities_skips_files_with_invalid_names(home):
    store.save(make_identity(home, "perso"))
    (paths.identities_dir() / "My Notes.gitconfig").write_text("", encoding="utf-8")
    assert [i.name for i in store.list_identities()] == ["perso"]


@pytest.mark.parametrize("name", ["", "Perso", "my id", "../x", "-x"])
def test_invalid_names_rejected(name):
    with pytest.raises(GitShiftError, match="invalid identity name"):
        store.validate_name(name)


@pytest.mark.parametrize("email", ["", "nope", "a@b", "a b@c.d"])
def test_invalid_emails_rejected(email):
    with pytest.raises(GitShiftError, match="invalid email"):
        store.validate_email(email)
