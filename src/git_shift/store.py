"""Identities: one git config file per identity, plus the rules that activate them."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import gitconfig, paths
from .errors import GitShiftError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    user_name: str
    folder: str  # folder rule: absolute, forward slashes, trailing "/"
    ssh_key: str | None = None
    gh: str | None = None

    @property
    def path(self) -> Path:
        return identity_path(self.name)


def identity_path(name: str) -> Path:
    return paths.identities_dir() / f"{name}.gitconfig"


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise GitShiftError(
            f'invalid identity name "{name}": use lowercase letters, digits, "-" and "_"'
        )


def validate_email(email: str) -> None:
    if not EMAIL_RE.match(email):
        raise GitShiftError(f'invalid email "{email}"')


def ssh_command(key_path: str) -> str:
    return f"ssh -i {shlex.quote(key_path)} -o IdentitiesOnly=yes"


def save(identity: Identity) -> None:
    path = identity.path
    if path.exists():
        path.unlink()
    entries = [("user.name", identity.user_name), ("user.email", identity.email)]
    if identity.ssh_key:
        entries.append(("core.sshCommand", ssh_command(identity.ssh_key)))
    entries.append(("gitshift.folder", identity.folder))
    if identity.ssh_key:
        entries.append(("gitshift.sshKey", identity.ssh_key))
    if identity.gh:
        entries.append(("gitshift.gh", identity.gh))
    for key, value in entries:
        gitconfig.set_value(key, value, file=path)


def load(name: str) -> Identity:
    validate_name(name)  # names become paths: "../../../" would point at ~/.gitconfig
    path = identity_path(name)
    if not path.is_file():
        raise GitShiftError(f'no identity named "{name}"')
    values = dict(gitconfig.list_entries(file=path))  # one git call per identity
    folder = values.get("gitshift.folder")
    return Identity(
        name=name,
        email=values.get("user.email", ""),
        user_name=values.get("user.name", ""),
        folder=paths.to_folder_rule(folder) if folder else "",  # normalises hand edits like ~/
        ssh_key=values.get("gitshift.sshkey"),
        gh=values.get("gitshift.gh"),
    )


def list_identities() -> list[Identity]:
    directory = paths.identities_dir()
    if not directory.is_dir():
        return []
    return [
        load(path.stem)
        for path in sorted(directory.glob("*.gitconfig"))
        if NAME_RE.match(path.stem)  # hand-made files git-shift could not have written
    ]
