import subprocess

import pytest
from helpers import GH_STATUS_SAMPLE, write_fake_gh

from git_shift import gh
from git_shift.gh import GhAccount

GH_STATUS_ACCOUNTS = [
    GhAccount("github.com", "alice", active=True, valid=True),
    GhAccount("github.com", "bob", active=False, valid=True),
    GhAccount("github.com", "carol", active=False, valid=False),
]

# CLICOLOR_FORCE=1 beats NO_COLOR: gh then bolds the login and the true/false value
COLOURED_SAMPLE = (
    "github.com\n"
    "  \x1b[0;32m✓\x1b[0m Logged in to github.com account \x1b[0;1;39malice\x1b[0m (keyring)\n"
    "  - Active account: \x1b[0;1;39mtrue\x1b[0m\n"
    "\n"
    "  \x1b[0;32m✓\x1b[0m Logged in to github.com account \x1b[0;1;39mbob\x1b[0m (keyring)\n"
    "  - Active account: \x1b[0;1;39mfalse\x1b[0m\n"
    "\n"
    "  \x1b[0;31mX\x1b[0m Failed to log in to github.com account \x1b[0;1;39mcarol\x1b[0m"
    " (default)\n"
    "  - Active account: \x1b[0;1;39mfalse\x1b[0m\n"
)

# gh's check of alice's token timed out, so it does not know whether the token is valid
TIMEOUT_SAMPLE = """github.com
  X Timeout trying to log in to github.com account alice (keyring)
  - Active account: true

  ✓ Logged in to github.com account bob (keyring)
  - Active account: false
  - Git operations protocol: https
"""

# A header without a login, between two accounts: its "Active account" line is its own
UNPARSEABLE_TEMPLATE = """github.com
  ✓ Logged in to github.com account alice (keyring)
  - Active account: false
  - Git operations protocol: https

  {header}
  - Active account: true

  ✓ Logged in to github.com account bob (keyring)
  - Active account: false
  - Git operations protocol: https
"""


def test_parse_multi_account_output():
    assert gh.parse_auth_status(GH_STATUS_SAMPLE) == GH_STATUS_ACCOUNTS


def test_parse_ignores_colour_codes():
    assert gh.parse_auth_status(COLOURED_SAMPLE) == GH_STATUS_ACCOUNTS


def test_parse_legacy_single_account_output():
    legacy = "github.com\n  ✓ Logged in to github.com as alice (oauth_token)\n"
    assert gh.parse_auth_status(legacy) == [GhAccount("github.com", "alice", True, True)]


def test_parse_timeout_counts_as_valid():
    assert gh.parse_auth_status(TIMEOUT_SAMPLE) == [
        GhAccount("github.com", "alice", active=True, valid=True),
        GhAccount("github.com", "bob", active=False, valid=True),
    ]


@pytest.mark.parametrize(
    "header",
    [
        "X Failed to log in to github.com using token (GH_TOKEN)",
        "X Timeout trying to log in to github.com using token (GH_TOKEN)",
    ],
)
def test_active_flag_of_unparseable_header_is_ignored(header):
    accounts = gh.parse_auth_status(UNPARSEABLE_TEMPLATE.format(header=header))
    assert accounts == [
        GhAccount("github.com", "alice", active=False, valid=True),
        GhAccount("github.com", "bob", active=False, valid=True),
    ]
    assert gh.active_login(accounts) is None


def test_active_login():
    assert gh.active_login(gh.parse_auth_status(GH_STATUS_SAMPLE)) == "alice"
    assert gh.active_login([]) is None


def test_accounts_is_none_without_gh(monkeypatch):
    monkeypatch.setattr(gh, "_executable", lambda: None)
    assert gh.accounts() is None


def test_accounts_is_none_when_gh_cannot_start(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_executable", lambda: str(tmp_path / "missing-gh"))
    assert gh.accounts() is None


def test_accounts_is_none_when_gh_times_out(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=20)

    monkeypatch.setattr(gh, "_executable", lambda: "gh")
    monkeypatch.setattr(gh.subprocess, "run", hang)
    assert gh.accounts() is None


def test_accounts_runs_gh(tmp_path, monkeypatch):
    fake = write_fake_gh(tmp_path, GH_STATUS_SAMPLE)
    monkeypatch.setattr(gh, "_executable", lambda: str(fake))
    assert gh.active_login(gh.accounts()) == "alice"


def test_accounts_reads_the_report_on_stderr(tmp_path, monkeypatch):
    # gh 2.40+ writes the report to stderr and exits 1 when an account failed or timed
    # out; gh before 2.40 always wrote it to stderr
    fake = write_fake_gh(tmp_path, GH_STATUS_SAMPLE, stream="stderr", exit_code=1)
    monkeypatch.setattr(gh, "_executable", lambda: str(fake))
    assert gh.accounts() == GH_STATUS_ACCOUNTS
