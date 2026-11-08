"""Identities: one git config file per identity, plus the rules that activate them."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitconfig, paths
from .errors import GitCommandError, GitShiftError

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")  # used with fullmatch
GIT_IDENT_CRUD = ",:;<>\"'\\"  # git strips these from both ends of a name or email
EMAIL_RE = re.compile(r'[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+')  # git deletes < and > anywhere
WINDOWS_RESERVED_RE = re.compile(r"con|prn|aux|nul|com[0-9]|lpt[0-9]")  # used with fullmatch
GH_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")  # used with fullmatch
IDENTITY_KEYS = ("user.name", "user.email", "core.sshcommand")
AUTHOR_OVERRIDE_KEYS = ("author.name", "author.email", "committer.name", "committer.email")


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


def _valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name)) and not WINDOWS_RESERVED_RE.fullmatch(name)


def validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise GitShiftError(
            f'invalid identity name "{name}": use lowercase letters, digits, "-" and "_", '
            "starting with a letter or digit"
        )
    if WINDOWS_RESERVED_RE.fullmatch(name):
        raise GitShiftError(f'invalid identity name "{name}": the name is reserved by Windows')


def validate_email(email: str) -> None:
    if not EMAIL_RE.fullmatch(email) or email.strip(GIT_IDENT_CRUD) != email:
        raise GitShiftError(f'invalid email "{email}"')


def ssh_command(key_path: str) -> str:
    # git runs core.sshCommand through sh, also on Windows, so POSIX quoting is right
    return f"ssh -i {shlex.quote(key_path)} -o IdentitiesOnly=yes"


def replace_file(src: Path, dst: Path) -> None:
    """os.replace, retried briefly on Windows, where a git process reading dst blocks it."""
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def temp_beside(path: Path) -> Path:
    """A new empty file next to path, unique to this run, so concurrent runs never share it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    return Path(name)


def save(identity: Identity) -> None:
    """Write the identity file in one step, so a failure never leaves a partial file."""
    validate_name(identity.name)
    path = identity.path
    tmp = temp_beside(path)
    entries = [("user.name", identity.user_name), ("user.email", identity.email)]
    if identity.ssh_key:
        entries.append(("core.sshCommand", ssh_command(identity.ssh_key)))
    entries.append(("gitshift.folder", identity.folder))
    if identity.ssh_key:
        entries.append(("gitshift.sshKey", identity.ssh_key))
    if identity.gh:
        entries.append(("gitshift.gh", identity.gh))
    try:
        for key, value in entries:
            gitconfig.set_value(key, value, file=tmp)
        replace_file(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


def identity_names() -> list[str]:
    """Names of the identity files git-shift could have written, sorted."""
    directory = paths.identities_dir()
    if not directory.is_dir():
        return []
    return sorted(
        path.stem
        for path in directory.glob("*.gitconfig")
        if path.is_file() and _valid_name(path.stem)
    )


def list_identities() -> list[Identity]:
    return [load(name) for name in identity_names()]


def rule_entries(identities: list[Identity]) -> list[tuple[str, str]]:
    """The (key, value) entries write_rules writes, in order. Deeper folders come last so
    they win."""
    usable = [i for i in identities if i.folder]  # an empty gitdir pattern matches every repo
    return [
        (
            f"includeIf.gitdir/i:{paths.gitdir_pattern(identity.folder)}.path",
            paths.to_git_path(identity.path),
        )
        for identity in sorted(usable, key=lambda i: (paths.depth(i.folder), i.name))
    ]


def write_rules(identities: list[Identity]) -> None:
    """Regenerate rules.gitconfig from the identities."""
    rules = paths.rules_file()
    tmp = temp_beside(rules)
    try:
        tmp.write_text(
            "# Managed by git-shift. Do not edit by hand.\n", encoding="utf-8", newline="\n"
        )
        for key, value in rule_entries(identities):
            gitconfig.set_value(key, value, file=tmp)
        replace_file(tmp, rules)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def global_config_file() -> Path | None:
    for candidate in paths.global_config_files():
        if candidate.is_file():
            return candidate
    return None


def _copy_to_backups(source: Path) -> Path:
    """A timestamped copy of a global config file in the backups dir."""
    backups = paths.backups_dir()
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"gitconfig-{time.strftime('%Y%m%d-%H%M%S')}"
    tmp = temp_beside(target)
    shutil.copy2(source, tmp)
    replace_file(tmp, target)
    return target


def backup_global_config() -> Path | None:
    """Copy the global config once, before git-shift changes it for the first time."""
    source = global_config_file()
    if source is None or any(paths.backups_dir().glob("gitconfig-*")):
        return None
    return _copy_to_backups(source)


def _include_target(value: str) -> str:
    """Absolute path of a global include.path value, resolved as git does: a relative path
    is relative to the config file that contains it."""
    value = paths.expand(value)
    base = global_config_file()
    if not _is_absolute(value) and base is not None:
        value = os.path.join(base.parent, value)
    return paths.to_git_path(value)


def _quoted(value: str) -> str:
    """value as a double-quoted git config string: # and ; then never start a comment."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def include_line() -> str:
    """The include of rules.gitconfig as one line of git config."""
    return f"[include] path = {_quoted(paths.to_git_path(paths.rules_file()))}"


def include_lands_last(file: Path) -> bool:
    """Whether `git config --add include.path` would put the rules after every entry of
    file. git adds to the file's last [include] section, which can come before a [user]
    section: tried on a copy, so git decides."""
    target = paths.to_git_path(paths.rules_file())
    with tempfile.TemporaryDirectory(prefix="git-shift-") as directory:
        copy = Path(directory) / "config"
        shutil.copyfile(file, copy)
        gitconfig.add_value("include.path", target, file=copy)
        return gitconfig.list_entries(file=copy)[-1:] == [("include.path", target)]


def _append_include(file: Path) -> None:
    """Append a new [include] section for the rules at the end of file.

    The one place git-shift writes the global config as text: `git config --add` would put
    the path in the file's last [include] section, and a [user] section after it would then
    beat every identity. It writes as git does: the new content goes into git's lock file,
    so a concurrent git config fails ("could not lock config file") instead of writing at
    the same time. git must then read the lock file as the old entries plus the include,
    or the real file stays untouched. The lock file then replaces the real one in one step,
    with its mode: the symlink to it (dotfiles) stays a symlink, and a crash or a full disk
    never leaves a partial line in it.
    """
    real = Path(os.path.realpath(file))
    lock = real.with_name(real.name + ".lock")
    where = paths.to_git_path(real)
    if not os.access(real, os.W_OK):  # whoever made their dotfiles read-only wants them kept
        raise GitShiftError(f"{where} is read-only: make it writable, then run the command again")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise GitShiftError(
            f"could not lock {where}: {lock.name} exists, so a git command may be writing it; "
            f"if no git command is running, delete {paths.to_git_path(lock)}"
        ) from exc
    except OSError as exc:
        raise GitShiftError(f"could not lock {where}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as out:
            data = real.read_bytes()
            newline = b"" if not data or data.endswith(b"\n") else b"\n"
            rules = _quoted(paths.to_git_path(paths.rules_file()))
            out.write(data + newline + f"[include]\n\tpath = {rules}\n".encode())
            out.flush()
            os.fsync(out.fileno())
        _check_appended(real, lock, where)
        _copy_to_backups(real)  # fresh: backup_global_config() copies only once ever
        shutil.copymode(real, lock)
        replace_file(lock, real)
    except BaseException as exc:
        stuck = _remove_lock(lock)
        note = f" ({paths.to_git_path(lock)} is left behind: delete it. {stuck})" if stuck else ""
        if isinstance(exc, GitShiftError):
            if not note:
                raise
            raise GitShiftError(f"{exc}{note}") from exc
        if isinstance(exc, OSError):
            raise GitShiftError(f"could not write {where}: {exc}{note}") from exc
        raise  # Ctrl-C and the like, as they are


def _remove_lock(lock: Path) -> OSError | None:
    """Remove our lock file, also once copymode made it read-only: Windows deletes no
    read-only file. What goes wrong is returned, never raised, so that it never hides the
    error that made us remove it. A lock left behind would fail every git config write."""
    try:
        if lock.exists():
            os.chmod(lock, stat.S_IREAD | stat.S_IWRITE)
        lock.unlink(missing_ok=True)
    except OSError as exc:
        return exc
    return None


def _check_appended(real: Path, lock: Path, where: str) -> None:
    """git must read the lock file as the real file's entries plus the include, nothing
    else: a last line ending with a backslash would continue into the new section."""
    target = paths.to_git_path(paths.rules_file())
    before = gitconfig.list_entries(file=real)
    try:
        after = gitconfig.list_entries(file=lock)
    except GitCommandError as exc:
        raise GitShiftError(
            f"did not change {where}: with the git-shift include appended, git cannot read "
            f"it: {exc.stderr}"
        ) from exc
    if after != [*before, ("include.path", target)]:
        raise GitShiftError(
            f"did not change {where}: with the git-shift include appended, git would read "
            'the end of that file differently. Check its last line (one ending with "\\" '
            "continues on the next line), then try again"
        )


def ensure_global_include() -> None:
    target = paths.to_git_path(paths.rules_file())
    current = gitconfig.get_all("include.path", scope="global")
    if target.casefold() in (_include_target(value).casefold() for value in current):
        return
    file = global_config_file()  # the one `git config --global` writes
    if file is None or include_lands_last(file):
        backup_global_config()
        gitconfig.add_value("include.path", target, scope="global")
    else:
        _append_include(file)  # with its own backup
    if keys_after_global_include() != []:  # None: missing; a key: read after the rules
        raise GitShiftError(
            f"added the git-shift include to {paths.to_git_path(file or '~/.gitconfig')}, "
            "but git does not read it after your other settings: run git shift doctor"
        )


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
            except (GitShiftError, OSError):
                continue  # unreadable or malformed: git reports it itself when the include applies
            late.update(k for k, _ in included if k in IDENTITY_KEYS)
    return sorted(late)


def global_author_overrides() -> list[str]:
    """author.* / committer.* keys in the global config: git uses them instead of user.* in
    every repository, whatever the identity."""
    entries = gitconfig.list_entries(scope="global")
    return sorted({key for key, _ in entries if key in AUTHOR_OVERRIDE_KEYS})


def inherited_ssh_keys(identities: list[Identity]) -> list[tuple[Identity, Identity]]:
    """(inner, outer) for each identity without an SSH key inside the folder of one with a
    key: git applies every matching rule, so inner uses the SSH key of outer, the deepest
    enclosing identity that has one."""
    usable = [i for i in identities if i.folder]
    pairs = []
    for inner in usable:
        if inner.ssh_key:
            continue
        outers = [
            o
            for o in usable
            if o is not inner and o.ssh_key and paths.is_within(inner.folder, o.folder)
        ]
        if outers:
            pairs.append((inner, max(outers, key=lambda o: paths.depth(o.folder))))
    return pairs


def inherited_ssh_key_text(inner: Identity, outer: Identity) -> str:
    return (
        f'"{inner.name}" is inside the folder of "{outer.name}" and has no SSH key, '
        f"so git uses {outer.ssh_key} there"
    )


def _inherited_ssh_key_warnings(new: Identity, existing: list[Identity]) -> list[str]:
    """The inherited keys that involve the new identity, on either side."""
    return [
        inherited_ssh_key_text(inner, outer)
        for inner, outer in inherited_ssh_keys([*existing, new])
        if new is inner or new is outer
    ]


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
    if gh is not None:
        gh = gh.removeprefix("@")
        if not GH_LOGIN_RE.fullmatch(gh):
            raise GitShiftError(f'invalid GitHub login "{gh}"')
    if identity_path(name).exists():
        raise GitShiftError(f'identity "{name}" already exists')
    folder_rule = paths.to_folder_rule(folder)
    existing = list_identities()
    for other in existing:
        if paths.fold_ascii(other.folder) == paths.fold_ascii(folder_rule):
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
        write_rules(list_identities())
    except BaseException:
        identity.path.unlink(missing_ok=True)  # the previous rules file is still in place
        raise
    late = keys_after_global_include()
    if late:
        warnings.append(
            f"your global config sets {', '.join(late)} after the git-shift include, "
            "so it can override your identities: run git shift doctor"
        )
    overrides = global_author_overrides()
    if overrides:
        warnings.append(
            f"your global config sets {', '.join(overrides)}, which git uses instead of "
            "the identity's name and email in every repository"
        )
    return identity, warnings


def remove_identity(name: str) -> None:
    validate_name(name)
    path = identity_path(name)
    if not path.is_file():
        raise GitShiftError(f'no identity named "{name}"')
    remaining = [load(other) for other in identity_names() if other != name]  # before changes
    path.unlink()
    write_rules(remaining)
