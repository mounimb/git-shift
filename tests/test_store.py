import dataclasses
import os

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
    store.save(make_identity(home, "pro-x"))
    store.save(make_identity(home, "pro"))
    store.save(make_identity(home, "perso"))
    assert [i.name for i in store.list_identities()] == ["perso", "pro", "pro-x"]


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
    (paths.identities_dir() / "x.gitconfig").mkdir()
    assert [i.name for i in store.list_identities()] == ["perso"]


@pytest.mark.parametrize("name", ["", "Perso", "my id", "../x", "-x", "perso\n"])
def test_invalid_names_rejected(name):
    with pytest.raises(GitShiftError, match="invalid identity name"):
        store.validate_name(name)


@pytest.mark.parametrize(
    "email",
    [
        "",
        "nope",
        "a@b",
        "a b@c.d",
        "a@b.c\n",
        "<me@example.com>",
        '"me@example.com"',
        "'me@example.com'",
        "me@example.com,",
        "me@example.com;",
        ":me@example.com",
    ],
)
def test_invalid_emails_rejected(email):
    with pytest.raises(GitShiftError, match="invalid email"):
        store.validate_email(email)


@pytest.mark.parametrize("name", ["perso", "work-2", "a_b", "0x"])
def test_valid_names_accepted(name):
    store.validate_name(name)


@pytest.mark.parametrize(
    "email", ["me@example.com", "first.last+tag@sub.example.co.uk", "o'brien@example.com"]
)
def test_valid_emails_accepted(email):
    store.validate_email(email)


def test_ssh_command_quotes_paths_with_spaces():
    assert store.ssh_command("C:/Users/John Doe/.ssh/id") == (
        "ssh -i 'C:/Users/John Doe/.ssh/id' -o IdentitiesOnly=yes"
    )


def test_save_overwrites_and_drops_removed_keys(home):
    identity = make_identity(home, ssh_key=paths.to_git_path(home / ".ssh" / "id"))
    store.save(identity)
    store.save(dataclasses.replace(identity, ssh_key=None))
    assert store.load("perso").ssh_key is None
    assert "sshCommand" not in identity.path.read_text(encoding="utf-8")
    assert not identity.path.with_name("perso.gitconfig.tmp").exists()


def test_save_rejects_invalid_names(home):
    with pytest.raises(GitShiftError, match="invalid identity name"):
        store.save(make_identity(home, name="../evil"))


def write_identity_file(name, text):
    paths.identities_dir().mkdir(parents=True, exist_ok=True)
    (paths.identities_dir() / f"{name}.gitconfig").write_text(text, encoding="utf-8")


def test_load_expands_home_in_a_hand_edited_folder(home):
    write_identity_file("perso", "[gitshift]\n\tfolder = ~/code\n")
    assert store.load("perso").folder == paths.to_folder_rule(home / "code")


@pytest.mark.parametrize("line", ["folder = code", "folder =", "folder"])
def test_load_treats_a_relative_or_empty_folder_as_missing(home, line):
    write_identity_file("perso", f"[gitshift]\n\t{line}\n")
    assert store.load("perso").folder == ""


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths exist only on Windows")
def test_load_treats_a_folder_without_a_drive_as_missing(home):
    write_identity_file("perso", "[gitshift]\n\tfolder = /c/Users/me/code\n")
    assert store.load("perso").folder == ""
