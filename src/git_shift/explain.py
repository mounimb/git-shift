"""What status and doctor say about a repository: where its email comes from, why it is not
the expected one, the SSH command and the gh account, each with its fix. The wording lives
here, apart from cli, so that doctor can share it: doctor cannot import cli (circular)."""

from __future__ import annotations

import os

from . import gh, paths, resolve, store

GH_HOST = "github.com"
GH_TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")  # gh prefers GH_TOKEN on github.com
PLAIN_SSH = "ssh"  # as core.sshCommand, it overrides a command inherited from elsewhere

PLACES = {
    "local": "this repo's .git/config",
    "worktree": "this worktree's config.worktree",
    "global": "your global git config",
    "command line": "a -c option or GIT_CONFIG_* variable",  # git reports both the same way
}


def place(eff: resolve.Effective) -> str:
    """Where an email set in git config comes from."""
    if eff.source == "identity":
        return f'identity "{eff.identity}"'
    return PLACES.get(eff.source) or eff.origin or eff.source


def describe_source(eff: resolve.Effective) -> str:
    if eff.source == "unset":
        return "nothing: no email configured"
    if eff.source == "environment":
        return f"the {eff.key} environment variable"  # GIT_AUTHOR_EMAIL or EMAIL
    if eff.key == "author.email":
        return f"author.email in {place(eff)}"
    return place(eff)


def email_text(eff: resolve.Effective) -> str:
    if eff.email is None:
        return "(unset)"
    return eff.email or "(empty)"


def mismatch(eff: resolve.Effective, expected: store.Identity, kind: str) -> str:
    """Why git commits with another email than the expected identity's. kind: how the
    identity matched (resolve.Match.kind)."""
    # first the overrides that a local user.email cannot beat
    if eff.key == "GIT_AUTHOR_EMAIL":
        return "GIT_AUTHOR_EMAIL is set in your environment"
    if eff.key == "author.email" or eff.source == "worktree":
        return f'{eff.key} in {place(eff)} replaces the email of "{expected.name}"'
    if eff.source == "command line":
        return (
            "a -c user.email option or GIT_CONFIG_* variable "
            f'replaces the email of "{expected.name}"'
        )
    if kind == resolve.REMOTE:
        if not expected.folder:
            return (
                f'this repo belongs to {expected.gh} on GitHub, but "{expected.name}" '
                "has no usable folder"
            )
        return f"this repo belongs to {expected.gh} on GitHub but is outside {expected.folder}"
    if eff.source == "unset":
        return "no email is configured here"
    if eff.source == "local":
        return "overridden by this repo's local config (.git/config)"
    if eff.source == "global" and kind == resolve.FOLDER:
        return "your global git config overrides git-shift rules: run git shift doctor"
    return f'the email comes from {describe_source(eff)}, not from "{expected.name}"'


def unset_global(key: str, origin: str) -> str:
    """The command that removes every value of key from a global config file (origin, in
    git path form). --unset-all: plain --unset fails on several values."""
    # git reads both global files, but --global only writes the first one that exists
    first = store.global_config_file()
    if first is not None and paths.to_git_path(first).casefold() == origin.casefold():
        return f"git config --global --unset-all {key}"
    # double quotes work in bash, PowerShell and cmd
    return f'git config --file "{origin}" --unset-all {key}'


def override_fix(eff: resolve.Effective) -> str | None:
    """The fix for an email that a local user.email cannot beat, or None when it can (then
    `git shift use --here` is the fix). --unset-all: plain --unset fails on several values."""
    if eff.key == "GIT_AUTHOR_EMAIL":
        return "unset the GIT_AUTHOR_EMAIL environment variable"  # the command depends on the shell
    if eff.source == "worktree":
        return f"git config --worktree --unset-all {eff.key}"
    if eff.source == "command line":
        return f"drop the -c {eff.key} option or GIT_CONFIG_* variable"
    if eff.key != "author.email":
        return None
    if eff.source == "local":
        return "git config --local --unset-all author.email"
    if eff.source == "global":
        return unset_global("author.email", eff.origin or "")
    return f"remove author.email from {eff.origin or eff.source}"


def mismatch_fix(eff: resolve.Effective, expected: store.Identity) -> str:
    return override_fix(eff) or f"git shift use {expected.name} --here"


def dangling_pin(
    cwd: str | os.PathLike[str], identities: list[store.Identity], match: resolve.Match | None
) -> tuple[str, str] | None:
    """(problem, fix) when the repository is pinned to an identity that cannot apply."""
    pin = resolve.pinned_name(cwd)
    if pin is None or (match is not None and match.kind == resolve.PIN):  # the pin applies
        return None
    if any(identity.name == pin for identity in identities):
        problem = "which has no email, so the pin is ignored"
    else:
        problem = "which does not exist"  # removed since, or a typo
    return f'this repo is pinned to "{pin}", {problem}', (
        f"git config --local --unset-all {resolve.PIN_KEY}"
    )


# gh: token variables are checked for presence, never shown


def gh_token_variables() -> list[str]:
    """The token variables set: gh then uses that token instead of its logins."""
    return [name for name in GH_TOKEN_VARIABLES if os.environ.get(name)]  # gh ignores ""


def unset_variables(names: list[str]) -> str:
    plural = "s" if len(names) > 1 else ""
    return f"unset the {' and '.join(names)} environment variable{plural}"


def gh_token_problem(tokens: list[str]) -> str:
    return f"{tokens[0]} is set, so gh uses that token instead of its logins"


def gh_login_problem(login: str, accounts: list[gh.GhAccount]) -> tuple[str, str] | None:
    """(problem, fix) when gh has no valid login for `login` on github.com."""
    known = [a for a in accounts if a.host == GH_HOST and a.login.casefold() == login.casefold()]
    if any(account.valid for account in known):
        return None
    if known:
        return f"the gh login of {login} is invalid or expired", f"gh auth login -h {GH_HOST}"
    return f"gh is not logged in as {login}", f"gh auth login -h {GH_HOST}"


def gh_mismatch(login: str, accounts: list[gh.GhAccount]) -> tuple[str | None, str]:
    """(problem, fix) when the active gh account on github.com is not `login`. problem is
    None when the line naming both accounts says it all."""
    tokens = gh_token_variables()
    if tokens:  # gh auth switch refuses to run while one is set
        return gh_token_problem(tokens), unset_variables(tokens)
    return gh_login_problem(login, accounts) or (None, f"gh auth switch -h {GH_HOST} -u {login}")


# SSH


def written_ssh_commands(identities: list[store.Identity]) -> set[str]:
    """The commands git-shift writes, in a folder rule or with use --here."""
    return {store.ssh_command(identity.ssh_key) for identity in identities if identity.ssh_key}


def pushes_use_the_wrong_key(
    identity: store.Identity, eff: resolve.Effective, identities: list[store.Identity]
) -> bool:
    """When status warns. Without a key, only another identity's command counts: any other
    is the user's own setup, and warning about it would repeat on every run."""
    if identity.ssh_key:
        return eff.ssh_command != store.ssh_command(identity.ssh_key)
    return eff.ssh_command in written_ssh_commands(identities)


def ssh_problem(identity: store.Identity, eff: resolve.Effective) -> str:
    """That SSH does not use what the identity expects. Nothing is unset automatically."""
    has = f"uses the key {identity.ssh_key}" if identity.ssh_key else "has no SSH key"
    command = eff.ssh_command or "plain ssh"
    return f'this repo still uses {command} for SSH, but "{identity.name}" {has}'


def ssh_fix(
    identity: store.Identity, eff: resolve.Effective, identities: list[store.Identity]
) -> str | None:
    """The fix status prints, only for a command git-shift wrote (a folder rule, an earlier
    pin): any other may be the user's own ssh program, which must stay. With a key, plain ssh
    would be just as wrong. Without one, plain ssh counts as "no key": the fix sets it."""
    if os.environ.get("GIT_SSH_COMMAND"):
        return "unset the GIT_SSH_COMMAND environment variable"
    if not identity.ssh_key and eff.ssh_command in written_ssh_commands(identities):
        return f"git config --local --replace-all core.sshCommand {PLAIN_SSH}"
    return None
