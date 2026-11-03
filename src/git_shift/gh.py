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
_SGR = re.compile(r"\x1b\[[0-9;]*m")
_DEBUG_ENV = ("GH_DEBUG", "DEBUG")  # os.environ keys are upper case on Windows


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
        # gh prints this only when the token check timed out, so the token's validity is
        # unknown: count it as valid rather than report a login as broken. (Offline, with a
        # DNS failure or a refused connection, gh says "Failed to log in" instead.)
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
    text = _SGR.sub("", text)  # colour codes, if gh was forced to print them
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
    # GH_DEBUG and DEBUG make gh print HTTP traces: keep them out of our pipes
    env = {name: value for name, value in os.environ.items() if name not in _DEBUG_ENV}
    env.update(
        CLICOLOR_FORCE="0",  # otherwise it wins over NO_COLOR, even when piped
        GH_NO_UPDATE_NOTIFIER="1",
        GH_PROMPT_DISABLED="1",
        GH_SPINNER_DISABLED="1",
        NO_COLOR="1",
    )
    try:
        proc = subprocess.run(
            [exe, "auth", "status"],
            stdin=subprocess.DEVNULL,  # gh never reads our stdin or waits on a prompt
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
