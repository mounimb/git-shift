"""Identities: one git config file per identity, plus the rules that activate them."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import gitconfig, paths
from .errors import GitShiftError

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")  # used with fullmatch
GIT_IDENT_CRUD = ",:;<>\"'\\"  # git strips these from both ends of a name or email
EMAIL_RE = re.compile(r'[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+')  # git deletes < and > anywhere


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    user_name: str
    folder: str  # folder rule: absolute, forward slashes, trailing "/"; "" if unusable
    ssh_key: str | None = None
    gh: str | None = None

    @property
    def path(self) -> Path:
        return identity_path(self.name)


def identity_path(name: str) -> Path:
    return paths.identities_dir() / f"{name}.gitconfig"


def validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise GitShiftError(
            f'invalid identity name "{name}": use lowercase letters, digits, "-" and "_", '
            "starting with a letter or digit"
        )


def validate_email(email: str) -> None:
    if not EMAIL_RE.fullmatch(email) or email.strip(GIT_IDENT_CRUD) != email:
        raise GitShiftError(f'invalid email "{email}"')


def ssh_command(key_path: str) -> str:
    # git runs core.sshCommand through sh, also on Windows, so POSIX quoting is right
    return f"ssh -i {shlex.quote(key_path)} -o IdentitiesOnly=yes"


def save(identity: Identity) -> None:
    """Write the identity file in one step, so a failure never leaves a partial file."""
    validate_name(identity.name)
    path = identity.path
    tmp = path.with_name(path.name + ".tmp")  # not matched by *.gitconfig
    tmp.unlink(missing_ok=True)
    entries = [("user.name", identity.user_name), ("user.email", identity.email)]
    if identity.ssh_key:
        entries.append(("core.sshCommand", ssh_command(identity.ssh_key)))
    entries.append(("gitshift.folder", identity.folder))
    if identity.ssh_key:
        entries.append(("gitshift.sshKey", identity.ssh_key))
    if identity.gh:
        entries.append(("gitshift.gh", identity.gh))
    for key, value in entries:
        gitconfig.set_value(key, value, file=tmp)
    os.replace(tmp, path)


def _is_absolute(path: str) -> bool:
    """os.path.isabs, but on Windows a path also needs a drive or a UNC share: "/x" is
    relative to the current drive (Python before 3.13 called it absolute)."""
    return os.path.isabs(path) and (os.name != "nt" or bool(os.path.splitdrive(path)[0]))


def load(name: str) -> Identity:
    validate_name(name)  # names become paths: "../../../" would point at ~/.gitconfig
    path = identity_path(name)
    if not path.is_file():
        raise GitShiftError(f'no identity named "{name}"')
    values = dict(gitconfig.list_entries(file=path))  # one git call per identity
    folder = values.get("gitshift.folder", "")
    return Identity(
        name=name,
        email=values.get("user.email", ""),
        user_name=values.get("user.name", ""),
        # "" when missing or relative; ~/ is expanded
        folder=paths.to_folder_rule(folder) if _is_absolute(paths.expand(folder)) else "",
        ssh_key=values.get("gitshift.sshkey"),
        gh=values.get("gitshift.gh"),
    )


def list_identities() -> list[Identity]:
    directory = paths.identities_dir()
    if not directory.is_dir():
        return []
    return [
        load(path.stem)
        for path in sorted(directory.glob("*.gitconfig"), key=lambda p: p.stem)
        if path.is_file() and NAME_RE.fullmatch(path.stem)  # skip files git-shift never writes
    ]
