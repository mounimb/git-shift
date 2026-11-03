import pytest
from helpers import GH_STATUS_SAMPLE, write_fake_gh

from git_shift import gh
from git_shift.gh import GhAccount

# gh could not reach github.com to check alice's token (network down or slow)
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
    assert gh.parse_auth_status(GH_STATUS_SAMPLE) == [
        GhAccount("github.com", "alice", active=True, valid=True),
        GhAccount("github.com", "bob", active=False, valid=True),
        GhAccount("github.com", "carol", active=False, valid=False),
    ]


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


def test_accounts_runs_gh(tmp_path, monkeypatch):
    fake = write_fake_gh(tmp_path, GH_STATUS_SAMPLE)
    monkeypatch.setattr(gh, "_executable", lambda: str(fake))
    assert gh.active_login(gh.accounts()) == "alice"
