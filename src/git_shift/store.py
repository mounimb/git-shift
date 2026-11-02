"""Identities: one git config file per identity, plus the rules that activate them."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import time
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


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried briefly on Windows, where a git process reading dst blocks it."""
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


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
    _replace(tmp, path)


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


def write_rules(identities: list[Identity]) -> None:
    """Regenerate rules.gitconfig. Deeper folders come last so they win."""
    rules = paths.rules_file()
    rules.parent.mkdir(parents=True, exist_ok=True)
    tmp = rules.with_name(rules.name + ".tmp")
    tmp.write_text("# Managed by git-shift. Do not edit by hand.\n", encoding="utf-8", newline="\n")
    usable = [i for i in identities if i.folder]  # an empty gitdir pattern matches every repo
    for identity in sorted(usable, key=lambda i: (paths.depth(i.folder), i.name)):
        gitconfig.set_value(
            f"includeIf.gitdir/i:{paths.gitdir_pattern(identity.folder)}.path",
            paths.to_git_path(identity.path),
            file=tmp,
        )
    _replace(tmp, rules)


def global_config_file() -> Path | None:
    for candidate in paths.global_config_files():
        if candidate.is_file():
            return candidate
    return None


def backup_global_config() -> Path | None:
    """Copy the global config once, before git-shift changes it for the first time."""
    source = global_config_file()
    backups = paths.backups_dir()
    if source is None or any(backups.glob("gitconfig-*")):
        return None
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"gitconfig-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(source, target)
    return target


def _include_target(value: str) -> str:
    """Absolute path of a global include.path value, resolved as git does: a relative path
    is relative to the config file that contains it."""
    value = paths.expand(value)
    base = global_config_file()
    if not _is_absolute(value) and base is not None:
        value = os.path.join(base.parent, value)
    return paths.to_git_path(value)


def ensure_global_include() -> None:
    target = paths.to_git_path(paths.rules_file())
    current = gitconfig.get_all("include.path", scope="global")
    if target.casefold() in (_include_target(value).casefold() for value in current):
        return
    backup_global_config()
    gitconfig.add_value("include.path", target, scope="global")


IDENTITY_KEYS = ("user.name", "user.email", "core.sshcommand")


def keys_after_global_include() -> list[str] | None:
    """Identity keys the global config sets after the git-shift include, directly or in a
    file it includes there (one level deep), or None when the include is missing. Those
    keys can override identities. Includes before git-shift's are not followed."""
    rules = paths.to_git_path(paths.rules_file()).casefold()
    entries = gitconfig.list_entries(scope="global")
    positions = [
        index
        for index, (key, value) in enumerate(entries)
        if key == "include.path" and _include_target(value).casefold() == rules
    ]
    if not positions:
        return None
    late = set()
    for key, value in entries[positions[-1] + 1 :]:
        if key in IDENTITY_KEYS:
            late.add(key)
        elif key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            try:
                included = gitconfig.list_entries(file=_include_target(value))
            except GitShiftError:
                continue  # unreadable or malformed: git reports it itself when the include applies
            late.update(k for k, _ in included if k in IDENTITY_KEYS)
    return sorted(late)


def _inherited_ssh_key_warnings(new: Identity, existing: list[Identity]) -> list[str]:
    """git applies every matching rule, so an identity without an SSH key inside another
    identity's folder uses the SSH key of the deepest enclosing identity that has one."""
    everyone = [i for i in [*existing, new] if i.folder]
    warnings = []
    for inner in everyone:
        if inner.ssh_key:
            continue
        outers = [
            o
            for o in everyone
            if o is not inner and o.ssh_key and paths.is_within(inner.folder, o.folder)
        ]
        if not outers:
            continue
        outer = max(outers, key=lambda o: paths.depth(o.folder))
        if new is inner or new is outer:
            warnings.append(
                f'"{inner.name}" is inside the folder of "{outer.name}" and has no SSH key, '
                f"so git uses {outer.ssh_key} there"
            )
    return warnings


def add_identity(
    name: str,
    *,
    email: str,
    user_name: str,
    folder: str,
    ssh_key: str | None = None,
    gh: str | None = None,
) -> tuple[Identity, list[str]]:
    validate_name(name)
    validate_email(email)
    if not user_name.strip():
        raise GitShiftError("author name cannot be empty")  # git refuses empty idents
    if not folder.strip():
        raise GitShiftError("folder cannot be empty")  # "" would mean the current folder
    if identity_path(name).exists():
        raise GitShiftError(f'identity "{name}" already exists')
    folder_rule = paths.to_folder_rule(folder)
    existing = list_identities()
    for other in existing:
        if other.folder.casefold() == folder_rule.casefold():
            raise GitShiftError(f'folder {folder_rule} is already used by "{other.name}"')

    warnings = []
    if not Path(folder_rule).is_dir():
        warnings.append(f"folder {folder_rule} does not exist yet")
    key = paths.to_git_path(ssh_key) if ssh_key else None
    if key and not Path(key).is_file():
        warnings.append(f"SSH key {key} not found")

    identity = Identity(name, email, user_name, folder_rule, key, gh)
    warnings.extend(_inherited_ssh_key_warnings(identity, existing))
    ensure_global_include()  # first: idempotent, and git ignores an include of a missing file
    save(identity)
    try:
        write_rules([*existing, identity])
    except BaseException:
        identity.path.unlink(missing_ok=True)  # the previous rules file is still in place
        raise
    late = keys_after_global_include()
    if late:
        warnings.append(
            f"your global config sets {', '.join(late)} after the git-shift include, "
            "so it can override your identities: run git shift doctor"
        )
    return identity, warnings


def remove_identity(name: str) -> Identity:
    identity = load(name)
    identity.path.unlink()
    write_rules(list_identities())
    return identity
