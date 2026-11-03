"""Work out which identity should apply in a directory, and which one actually does."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitconfig, paths, remotes
from .store import Identity


@dataclass(frozen=True)
class Effective:
    email: str | None
    name: str | None
    ssh_command: str | None
    source: str  # identity | local | global | environment | command line | other | unset
    identity: str | None = None
    origin: str | None = None
    key: str | None = None  # GIT_AUTHOR_EMAIL | author.email | user.email | EMAIL; None if unset


def expected_identity(path, identities: list[Identity]) -> Identity | None:
    # an identity without an email is unusable, like one without a folder: it never matches
    matches = [
        identity
        for identity in identities
        if identity.email and paths.is_within(path, identity.folder)
    ]
    return max(matches, key=lambda identity: paths.depth(identity.folder), default=None)


def remote_identity(url: str, identities: list[Identity]) -> Identity | None:
    remote = remotes.parse_remote(url)
    if remote is None or remote.host != "github.com":
        return None
    for identity in identities:
        if identity.email and identity.gh and identity.gh.casefold() == remote.owner.casefold():
            return identity
    return None


def repo_root(cwd) -> str | None:
    proc = gitconfig.run_git("rev-parse", "--show-toplevel", cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def git_dir(cwd) -> str | None:
    proc = gitconfig.run_git("rev-parse", "--absolute-git-dir", cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def origin_url(cwd) -> str | None:
    # get-url expands insteadOf/pushInsteadOf and follows pushurl, like the URL git hands
    # to pre-push, so commit-time and push-time checks agree.
    proc = gitconfig.run_git("remote", "get-url", "--push", "origin", cwd=cwd, check=False)
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def expected_for_repo(
    cwd, identities: list[Identity], remote_url: str | None = None
) -> tuple[Identity, str] | None:
    """The identity a repository must use, and the reason (folder first, then remote).

    Folders are matched against the git directory, like includeIf gitdir, so worktrees
    and separate git dirs agree with what git applies.
    """
    location = git_dir(cwd) or str(cwd)
    identity = expected_identity(location, identities)
    if identity is not None:
        return identity, f"folder {identity.folder}"
    url = remote_url or origin_url(cwd)
    identity = remote_identity(url, identities) if url else None
    if identity is not None:
        return identity, f"GitHub owner {identity.gh}"
    return None


def _local_config(cwd) -> str | None:
    # --git-common-dir is relative to cwd (or absolute); a worktree shares the main config
    proc = gitconfig.run_git("rev-parse", "--git-common-dir", cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return paths.to_git_path(Path(cwd) / proc.stdout.strip() / "config")


def _classify(origin: gitconfig.Origin, identities: list[Identity], cwd) -> tuple[str, str | None]:
    if origin.kind != "file":
        return origin.kind, None
    path = paths.to_git_path(origin.path).casefold()  # origins are absolute
    for identity in identities:
        if paths.to_git_path(identity.path).casefold() == path:
            return "identity", identity.name
    if path == (_local_config(cwd) or "").casefold():
        return "local", None
    global_files = paths.global_config_files()
    if path in {paths.to_git_path(file).casefold() for file in global_files}:
        return "global", None
    return "other", None


def _author_name(cwd) -> str | None:
    """The name git commits with: GIT_AUTHOR_NAME, then author.name, then user.name."""
    env_name = os.environ.get("GIT_AUTHOR_NAME")
    if env_name is not None:
        return env_name
    # git skips an empty author.name
    return gitconfig.get("author.name", cwd=cwd) or gitconfig.get("user.name", cwd=cwd)


def effective(cwd, identities: list[Identity]) -> Effective:
    """The email git commits with in cwd, and where it comes from.

    Same order as git: GIT_AUTHOR_EMAIL, then author.email (whatever file it is in),
    then user.email, then EMAIL. So status never names an identity git does not use.
    """
    name = _author_name(cwd)
    ssh = gitconfig.get("core.sshCommand", cwd=cwd)
    env_email = os.environ.get("GIT_AUTHOR_EMAIL")
    if env_email is not None:  # git uses it even when empty
        return Effective(env_email, name, ssh, "environment", key="GIT_AUTHOR_EMAIL")
    key, origin = "author.email", gitconfig.get_with_origin("author.email", cwd=cwd)
    configured = origin is not None  # any email key, even empty, stops git reading EMAIL
    if origin is None or not origin.value:  # git skips an empty author.email
        key, origin = "user.email", gitconfig.get_with_origin("user.email", cwd=cwd)
    if origin is not None:
        source, identity = _classify(origin, identities, cwd)
        return Effective(origin.value, name, ssh, source, identity, origin.path, key)
    env_email = os.environ.get("EMAIL")
    if env_email and not configured and gitconfig.get("committer.email", cwd=cwd) is None:
        return Effective(env_email, name, ssh, "environment", key="EMAIL")
    return Effective(None, name, ssh, "unset")
