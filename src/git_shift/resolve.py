"""Work out which identity should apply in a directory, and which one actually does."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitconfig, paths, remotes
from .errors import GitShiftError
from .store import GIT_IDENT_CRUD, Identity

# ssh.github.com is GitHub's SSH over port 443 (ssh://git@ssh.github.com:443/owner/repo)
_GITHUB_HOSTS = ("github.com", "www.github.com", "ssh.github.com")
_EMAIL_CRUD = "".join(map(chr, range(33))) + GIT_IDENT_CRUD  # git's crud(): also c <= 32
_EMAIL_DELIMITERS = str.maketrans("", "", "<>\n")
PIN_KEY = "gitshift.identity"  # written to the repo's own config by `git shift use --here`
PIN_REASON = "pin in .git/config"
_WORKTREE_CONFIG = "config.worktree"
# git's words, read with LC_ALL=C because they can be translated. With "fatal:", so a folder
# named like this in another message (dubious ownership quotes the path) does not match.
_NOT_A_REPOSITORY = "fatal: not a git repository"
_NO_WORK_TREE = "fatal: this operation must be run in a work tree"


@dataclass(frozen=True)
class Effective:
    email: str | None
    name: str | None
    ssh_command: str | None
    source: str  # identity | local | worktree | global | environment | command line | other | unset
    identity: str | None = None
    origin: str | None = None  # config file (git path form) when the email comes from one
    key: str | None = None  # GIT_AUTHOR_EMAIL | author.email | user.email | EMAIL; None if unset


def expected_identity(path: str | os.PathLike[str], identities: list[Identity]) -> Identity | None:
    # an identity without an email is unusable, like one without a folder: it never matches
    matches = [
        identity
        for identity in identities
        if identity.email and paths.is_within(path, identity.folder)
    ]
    # write_rules orders rules by (depth, name) and git applies the last match: same here
    return max(matches, key=lambda i: (paths.depth(i.folder), i.name), default=None)


def remote_identity(url: str, identities: list[Identity]) -> Identity | None:
    remote = remotes.parse_remote(url)
    if remote is None or remote.host not in _GITHUB_HOSTS:
        return None
    for identity in identities:
        if identity.email and identity.gh and identity.gh.casefold() == remote.owner.casefold():
            return identity
    return None


def _probe(cwd: str | os.PathLike[str], option: str, *absent: str) -> str | None:
    """What `git rev-parse option` prints, or None if it fails with one of the absent messages.

    Any other failure means git refuses to work here (dubious ownership, unknown repository
    extension, bad config), which must not read as "no repository". It raises with all git
    said: for dubious ownership, the following lines give the safe.directory fix.
    """
    proc = gitconfig.run_git("rev-parse", option, cwd=cwd, check=False, env={"LC_ALL": "C"})
    if proc.returncode == 0:
        return proc.stdout.strip() or None
    stderr = proc.stderr.strip()
    if any(message in stderr.lower() for message in absent):  # git < 2.16: "Not a git ..."
        return None
    raise GitShiftError(f"git failed here:\n{stderr or f'exit code {proc.returncode}'}")


def repo_root(cwd: str | os.PathLike[str]) -> str | None:
    """The top of the working tree, or None outside a repository or without a working tree
    (a bare repository, or inside .git). Raises when git refuses to work here."""
    return _probe(cwd, "--show-toplevel", _NOT_A_REPOSITORY, _NO_WORK_TREE)


def git_dir(cwd: str | os.PathLike[str]) -> str | None:
    """The git directory, or None outside a repository. Raises when git refuses to work here."""
    return _probe(cwd, "--absolute-git-dir", _NOT_A_REPOSITORY)


def origin_url(cwd: str | os.PathLike[str]) -> str | None:
    # get-url expands insteadOf/pushInsteadOf and follows pushurl, like the URL git hands
    # to pre-push, so commit-time and push-time checks agree.
    proc = gitconfig.run_git("remote", "get-url", "--push", "origin", cwd=cwd, check=False)
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def _read_pin(cwd: str | os.PathLike[str]) -> str | None:
    # --local reads only the repository's shared config (every worktree of a clone sees
    # it), never the global one; it fails outside a repository, so callers check first
    return gitconfig.get(PIN_KEY, scope="local", cwd=cwd) or None


def pinned_name(cwd: str | os.PathLike[str]) -> str | None:
    """The identity name `use --here` pinned on the repository in cwd, if any. It may name
    an identity that no longer exists: expected_for_repo then ignores it."""
    return _read_pin(cwd) if git_dir(cwd) is not None else None


def expected_for_repo(
    cwd: str | os.PathLike[str], identities: list[Identity], remote_url: str | None = None
) -> tuple[Identity, str] | None:
    """The identity a repository must use, and the reason: the repository's pin first,
    then its folder, then its remote.

    A pin naming a missing identity, or one without an email, is ignored. Folders are
    matched against the git directory, like includeIf gitdir, so worktrees and separate
    git dirs agree with what git applies.
    """
    directory = git_dir(cwd)
    # the pin costs one git call: the guard runs this on every commit
    name = _read_pin(cwd) if directory is not None else None
    pinned = [identity for identity in identities if identity.name == name and identity.email]
    if pinned:
        return pinned[0], PIN_REASON
    location = directory or os.fspath(cwd)
    identity = expected_identity(location, identities)
    if identity is not None:
        return identity, f"folder {identity.folder}"
    url = remote_url or origin_url(cwd)
    identity = remote_identity(url, identities) if url else None
    if identity is not None:
        return identity, f"GitHub owner {identity.gh}"
    return None


def _local_config(cwd: str | os.PathLike[str]) -> str | None:
    # --git-common-dir is relative to cwd (or absolute); a worktree shares the main config
    proc = gitconfig.run_git("rev-parse", "--git-common-dir", cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return paths.to_git_path(Path(cwd) / proc.stdout.strip() / "config")


def _worktree_config(cwd: str | os.PathLike[str]) -> str | None:
    # --git-path is relative to cwd (or absolute); each worktree has its own file
    proc = gitconfig.run_git("rev-parse", "--git-path", _WORKTREE_CONFIG, cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return paths.to_git_path(Path(cwd) / proc.stdout.strip())


def _classify(
    origin: gitconfig.Origin, identities: list[Identity], cwd: str | os.PathLike[str]
) -> tuple[str, str | None]:
    if origin.kind != "file":
        return origin.kind, None
    path = paths.to_git_path(origin.path).casefold()  # origins are absolute
    for identity in identities:
        if paths.to_git_path(identity.path).casefold() == path:
            return "identity", identity.name
    if path == (_local_config(cwd) or "").casefold():
        return "local", None
    # the name decides first, so an ordinary config costs no extra git call
    if os.path.basename(path) == _WORKTREE_CONFIG:
        if path == (_worktree_config(cwd) or "").casefold():
            return "worktree", None
    global_files = paths.global_config_files()
    if path in {paths.to_git_path(file).casefold() for file in global_files}:
        return "global", None
    return "other", None


def _clean_email(email: str) -> str:
    """The email as git records it: crud stripped from both ends, then <, > and newlines
    dropped anywhere (ident.c, strbuf_addstr_without_crud)."""
    return email.strip(_EMAIL_CRUD).translate(_EMAIL_DELIMITERS)


def _author_name(cwd: str | os.PathLike[str]) -> str | None:
    """The name git commits with: GIT_AUTHOR_NAME, then author.name, then user.name."""
    env_name = os.environ.get("GIT_AUTHOR_NAME")
    if env_name is not None:
        return env_name
    # git skips an empty author.name
    return gitconfig.get("author.name", cwd=cwd) or gitconfig.get("user.name", cwd=cwd)


def effective(cwd: str | os.PathLike[str], identities: list[Identity]) -> Effective:
    """The email git commits with in cwd (cleaned as git records it), and where it comes from.

    Same order as git: GIT_AUTHOR_EMAIL, then author.email (whatever file it is in),
    then user.email, then EMAIL. So status never names an identity git does not use.
    Inside a pre-commit hook git exports GIT_AUTHOR_NAME/EMAIL, so there this always
    reports the environment: hooks should read `git var GIT_AUTHOR_IDENT` instead.
    """
    name = _author_name(cwd)
    ssh = os.environ.get("GIT_SSH_COMMAND") or gitconfig.get("core.sshCommand", cwd=cwd)
    env_email = os.environ.get("GIT_AUTHOR_EMAIL")
    if env_email is not None:  # git uses it even when empty
        email = _clean_email(env_email)
        return Effective(email, name, ssh, "environment", key="GIT_AUTHOR_EMAIL")
    key, origin = "author.email", gitconfig.get_with_origin("author.email", cwd=cwd)
    configured = origin is not None  # any email key, even empty, stops git reading EMAIL
    if origin is None or not origin.value:  # git skips an empty author.email
        key, origin = "user.email", gitconfig.get_with_origin("user.email", cwd=cwd)
    if origin is not None:
        source, identity = _classify(origin, identities, cwd)
        location = paths.to_git_path(origin.path) if origin.kind == "file" else None
        email = _clean_email(origin.value)
        return Effective(email, name, ssh, source, identity, location, key)
    env_email = os.environ.get("EMAIL")
    if env_email and not configured and gitconfig.get("committer.email", cwd=cwd) is None:
        return Effective(_clean_email(env_email), name, ssh, "environment", key="EMAIL")
    return Effective(None, name, ssh, "unset")
