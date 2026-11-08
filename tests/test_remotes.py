import pytest

from git_shift.remotes import Remote, parse_remote, uses_ssh


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mounimb/git-shift",
        "https://github.com/mounimb/git-shift.git",
        "https://user@github.com/mounimb/git-shift.git",
        "git@github.com:mounimb/git-shift.git",
        "ssh://git@github.com/mounimb/git-shift.git",
        "ssh://git@github.com:22/mounimb/git-shift",
        "github.com:mounimb/git-shift",
        "https://x-access-token:TOKEN@github.com/mounimb/git-shift.git",
        "https://github.com/mounimb/git-shift/",
        "  git@github.com:mounimb/git-shift.git\n",
        "git+ssh://git@github.com/mounimb/git-shift.git",
        "ssh+git://git@github.com/mounimb/git-shift",
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


def test_ssh_alias_host_is_kept_verbatim():
    # git@github-work:... aliases are not resolved; the remote check only knows github.com
    assert parse_remote("git@github-work:o/r").host == "github-work"


@pytest.mark.parametrize(
    ("url", "ssh"),
    [
        ("git@github.com:alice/app.git", True),
        ("github.com:alice/app", True),
        ("git@example.com:/srv/app.git", True),
        ("ssh://git@github.com/alice/app.git", True),
        ("SSH://git@github.com/alice/app.git", True),
        ("git+ssh://git@github.com/alice/app.git", True),
        ("ssh+git://git@github.com/alice/app.git", True),
        ("https://github.com/alice/app.git", False),
        ("git://example.com/app.git", False),
        ("file:///srv/app.git", False),
        ("/srv/app.git", False),
        ("C:/repos/app", False),
        (r"C:\repos\app", False),
        ("../app", False),
        ("", False),
    ],
)
def test_uses_ssh(url, ssh):
    assert uses_ssh(url) is ssh
