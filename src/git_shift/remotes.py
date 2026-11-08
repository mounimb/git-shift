"""Parse git remote URLs into host / owner / repository."""

from __future__ import annotations

import re
from dataclasses import dataclass

_URL = re.compile(
    r"^(?:https?|ssh|git|git\+ssh|ssh\+git)://(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?/(?P<path>.+)$"
)
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


@dataclass(frozen=True)
class Remote:
    host: str
    owner: str
    repo: str


def parse_remote(url: str) -> Remote | None:
    """Split a network remote into host (lowercased) / owner (first path segment) / repo.

    Returns None for local paths and unrecognised forms; callers treat None as "no check".
    """
    url = url.strip()
    match = _URL.match(url) or _SCP.match(url)
    if match is None:
        return None
    parts = [part for part in match.group("path").split("/") if part]
    if len(parts) < 2:
        return None
    repo = parts[-1].removesuffix(".git")
    return Remote(match.group("host").lower(), parts[0], repo)


_SSH_SCHEME = re.compile(r"^(?:ssh|git\+ssh|ssh\+git)://", re.IGNORECASE)
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):")  # a colon before any slash


def uses_ssh(url: str) -> bool:
    """Whether git reaches this remote over SSH: an ssh:// URL, or the scp-like
    [user@]host:path. A one-letter "host" is a Windows drive (C:/repo), a local path."""
    url = url.strip()
    if "://" in url:
        return bool(_SSH_SCHEME.match(url))
    match = _SCP_LIKE.match(url)
    return match is not None and len(match.group("host")) > 1
