"""Read-only view of the GitHub CLI's logged-in accounts."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from dataclasses import dataclass

from . import paths

_LOGGED_IN = re.compile(r"Logged in to (\S+) (?:account|as) (\S+)")
_FAILED = re.compile(r"Failed to log in to (\S+) account (\S+)")
_TIMEOUT = re.compile(r"Timeout trying to log in to (\S+) account (\S+)")
_ACTIVE = re.compile(r"- Active account: (true|false)")


@dataclass(frozen=True)
class GhAccount:
    host: str
    login: str
    active: bool
    valid: bool


def _header(line: str) -> GhAccount | None:
    """The account an account header line names, None for any other line."""
    if match := _LOGGED_IN.search(line):
        valid = True
    elif match := _FAILED.search(line):
        valid = False
    elif match := _TIMEOUT.search(line):
        # gh could not reach the host to check the token, so its validity is unknown.
        # Count it as valid: a network problem must not be reported as a broken login.
        valid = True
    else:
        return None
    return GhAccount(match.group(1), match.group(2), active=False, valid=valid)


def parse_auth_status(text: str) -> list[GhAccount]:
    """Accounts in `gh auth status` output, in the order gh lists them.

    gh 2.40+ prints a header line per account, followed by "- " detail lines, one of
    them "- Active account: true|false". That flag only applies to the account whose
    header opened the block. Any other line (a header without a login, such as "Failed
    to log in to H using token (GH_TOKEN)", a host line, a blank line) closes the block,
    so the flag after it is ignored rather than given to the previous account.
    """
    accounts: list[GhAccount] = []
    current: int | None = None  # index of the account whose detail lines are being read
    saw_active_flag = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-"):
            if match := _ACTIVE.fullmatch(line):
                saw_active_flag = True
                if current is not None:
                    active = match.group(1) == "true"
                    accounts[current] = dataclasses.replace(accounts[current], active=active)
            continue
        account = _header(line)
        if account is None:
            current = None
        else:
            accounts.append(account)
            current = len(accounts) - 1
    if not saw_active_flag:  # gh < 2.40: one account per host, always the active one
        accounts = [dataclasses.replace(account, active=account.valid) for account in accounts]
    return accounts


def _executable() -> str | None:
    return paths.find_executable("gh")  # never from the current directory


def accounts() -> list[GhAccount] | None:
    """Accounts known to gh, or None when gh is not installed or does not answer.

    git-shift never reads tokens: gh's output is parsed here and dropped, never kept.
    """
    exe = _executable()
    if exe is None:
        return None
    env = {
        **os.environ,
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PROMPT_DISABLED": "1",
        "GH_SPINNER_DISABLED": "1",
        "NO_COLOR": "1",
    }
    try:
        proc = subprocess.run(
            [exe, "auth", "status"],
            stdin=subprocess.DEVNULL,  # inside hooks, stdin belongs to git (pre-push refs)
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_auth_status(proc.stdout + "\n" + proc.stderr)


def active_login(accounts: list[GhAccount], host: str = "github.com") -> str | None:
    """Login of the active, valid account on `host`, if any."""
    for account in accounts:
        if account.host == host and account.active and account.valid:
            return account.login
    return None
