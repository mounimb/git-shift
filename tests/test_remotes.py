import pytest

from git_shift.remotes import Remote, parse_remote


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mounimb/git-shift",
        "https://github.com/mounimb/git-shift.git",
        "https://user@github.com/mounimb/git-shift.git",
        "git@github.com:mounimb/git-shift.git",
        "ssh://git@github.com/mounimb/git-shift.git",
        "ssh://git@github.com:22/mounimb/git-shift",
    ],
)
def test_parses_github_urls(url):
    assert parse_remote(url) == Remote("github.com", "mounimb", "git-shift")


def test_host_is_lowercased():
    assert parse_remote("https://GitHub.com/a/b").host == "github.com"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/srv/git/repo.git",
        "C:/repos/x",
        "C:\\repos\\x",
        "file:///tmp/x.git",
        "https://github.com/only-owner",
    ],
)
def test_rejects_local_paths_and_incomplete_urls(url):
    assert parse_remote(url) is None
