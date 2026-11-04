"""Command-line interface for git-shift."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from . import __version__, gh, gitconfig, paths, resolve, store
from .errors import GitShiftError

OK, WARN, ERR = "✓", "!", "✗"
GH_HOST = "github.com"
GH_TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")  # gh prefers GH_TOKEN on github.com
PLAIN_SSH = "ssh"  # as core.sshCommand, it overrides a command inherited from elsewhere


def cmd_add(args: argparse.Namespace) -> int:
    identity, warnings = store.add_identity(
        args.name,
        email=args.email,
        user_name=args.user_name,
        folder=args.folder,
        ssh_key=args.ssh_key,
        gh=args.gh,
    )
    print(f'{OK} identity "{identity.name}" created')
    print(f"  repos under {identity.folder} now commit as {identity.email}")
    for warning in warnings:
        print(f"{WARN} {warning}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    identities = store.list_identities()
    if not identities:
        print("no identities yet. Create one with:")
        print('  git shift add <identity> --email <email> --name "<your name>" --folder <dir>')
        return 0
    width = max(len(identity.name) for identity in identities)
    for identity in identities:
        folder = identity.folder or "(no usable folder)"  # hand-edited file, see doctor
        gh_part = f"  gh: {identity.gh}" if identity.gh else ""
        print(f"{identity.name:<{width}}  {identity.email}  {folder}{gh_part}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    store.remove_identity(args.name)
    print(f'{OK} identity "{args.name}" removed')
    return 0


_PLACES = {
    "local": "this repo's .git/config",
    "worktree": "this worktree's config.worktree",
    "global": "your global git config",
    "command line": "a -c option or GIT_CONFIG_* variable",  # git reports both the same way
}


def _place(eff: resolve.Effective) -> str:
    """Where an email set in git config comes from."""
    if eff.source == "identity":
        return f'identity "{eff.identity}"'
    return _PLACES.get(eff.source) or eff.origin or eff.source


def _describe_source(eff: resolve.Effective) -> str:
    if eff.source == "unset":
        return "nothing: no email configured"
    if eff.source == "environment":
        return f"the {eff.key} environment variable"  # GIT_AUTHOR_EMAIL or EMAIL
    if eff.key == "author.email":
        return f"author.email in {_place(eff)}"
    return _place(eff)


def _email_text(eff: resolve.Effective) -> str:
    if eff.email is None:
        return "(unset)"
    return eff.email or "(empty)"


def _explain_mismatch(eff: resolve.Effective, expected: store.Identity, reason: str) -> str:
    # first the overrides that a local user.email cannot beat
    if eff.key == "GIT_AUTHOR_EMAIL":
        return "GIT_AUTHOR_EMAIL is set in your environment"
    if eff.key == "author.email" or eff.source == "worktree":
        return f'{eff.key} in {_place(eff)} replaces the email of "{expected.name}"'
    if eff.source == "command line":
        return (
            "a -c user.email option or GIT_CONFIG_* variable "
            f'replaces the email of "{expected.name}"'
        )
    if reason.startswith("GitHub owner"):
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
    if eff.source == "global" and reason.startswith("folder "):
        return "your global git config overrides git-shift rules: run git shift doctor"
    return f'the email comes from {_describe_source(eff)}, not from "{expected.name}"'


def _override_fix(eff: resolve.Effective) -> str | None:
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
        # git reads both global files, but --global only writes the first one that exists
        first = store.global_config_file()
        if first is not None and paths.to_git_path(first).casefold() == eff.origin.casefold():
            return "git config --global --unset-all author.email"
        # double quotes work in bash, PowerShell and cmd
        return f'git config --file "{eff.origin}" --unset-all author.email'
    return f"remove author.email from {eff.origin or eff.source}"


def _print_dangling_pin(cwd: str, identities: list[store.Identity], reason: str) -> None:
    pin = resolve.pinned_name(cwd)
    if pin is None or reason == resolve.PIN_REASON:  # no pin, or the pin applies
        return
    if any(identity.name == pin for identity in identities):
        problem = "which has no email, so the pin is ignored"
    else:
        problem = "which does not exist"  # removed since, or a typo
    print(f'{WARN} this repo is pinned to "{pin}", {problem}')
    print(f"  fix: git config --local --unset-all {resolve.PIN_KEY}")


def _print_gh(expected: store.Identity | None) -> None:
    """The gh account against the one the identity expects. Advice only: never changes
    the exit code. Token variables are checked for presence, never shown."""
    if expected is None or not expected.gh:
        return
    accounts = gh.accounts()
    if accounts is None:
        return
    login = expected.gh
    active = gh.active_login(accounts, GH_HOST)
    if active is not None and active.casefold() == login.casefold():
        print(f"gh:       {active} (active)")
        return
    print(f"gh:       active account is {active or 'none'}, expected {login}")
    tokens = [name for name in GH_TOKEN_VARIABLES if os.environ.get(name)]  # gh ignores ""
    known = [a for a in accounts if a.host == GH_HOST and a.login.casefold() == login.casefold()]
    if tokens:  # gh auth switch refuses to run while one is set
        problem = f"{tokens[0]} is set, so gh uses that token instead of its logins"
        plural = "s" if len(tokens) > 1 else ""
        fix = f"unset the {' and '.join(tokens)} environment variable{plural}"
    elif any(account.valid for account in known):
        problem = None  # the line above says it all
        fix = f"gh auth switch -h {GH_HOST} -u {login}"
    elif known:
        problem = f"the gh login of {login} is invalid or expired"
        fix = f"gh auth login -h {GH_HOST}"
    else:
        problem = f"gh is not logged in as {login}"
        fix = f"gh auth login -h {GH_HOST}"
    if problem:
        print(f"{WARN} {problem}")
    print(f"  fix: {fix}")


def _print_ssh_warning(
    identity: store.Identity, eff: resolve.Effective, identities: list[store.Identity]
) -> None:
    """Warn when SSH does not use what the identity expects. Nothing is unset automatically.

    The fix is only printed for a command git-shift wrote (a folder rule, an earlier pin):
    any other may be the user's own ssh program, which must stay. With a key, it would also
    overwrite the command just written. Plain ssh counts as "no key": the fix sets it.
    """
    expected = store.ssh_command(identity.ssh_key) if identity.ssh_key else PLAIN_SSH
    if not eff.ssh_command or eff.ssh_command == expected:
        return
    has = f"uses the key {identity.ssh_key}" if identity.ssh_key else "has no SSH key"
    print(f'{WARN} this repo still uses {eff.ssh_command} for SSH, but "{identity.name}" {has}')
    written = {store.ssh_command(other.ssh_key) for other in identities if other.ssh_key}
    if os.environ.get("GIT_SSH_COMMAND"):
        print("  fix: unset the GIT_SSH_COMMAND environment variable")
    elif not identity.ssh_key and eff.ssh_command in written:
        print(f"  fix: git config --local --replace-all core.sshCommand {PLAIN_SSH}")


def _repo_text(cwd: str, root: str | None) -> str:
    if root:
        return root
    directory = resolve.git_dir(cwd)  # a bare repository, or inside .git
    return f"{directory} (no working tree)" if directory else "(not a git repository)"


def cmd_status(args: argparse.Namespace) -> int:
    cwd = os.getcwd()
    identities = store.list_identities()
    root = resolve.repo_root(cwd)
    found = resolve.expected_for_repo(cwd, identities)
    expected, reason = found if found else (None, "")
    print(f"repo:     {_repo_text(cwd, root)}")
    if expected is None:
        print("expected: no identity matches this location")
    else:
        print(f'expected: "{expected.name}" <{expected.email}>  (matched by {reason})')
    if root is None:
        return 0
    _print_dangling_pin(cwd, identities, reason)
    eff = resolve.effective(cwd, identities)
    print(f"email:    {_email_text(eff)}  (from {_describe_source(eff)})")
    if eff.ssh_command:
        print(f"ssh:      {eff.ssh_command}")
    code = 0
    if expected is not None and not resolve.same_email(eff.email, expected.email):
        print(f"{ERR} {_explain_mismatch(eff, expected, reason)}")
        print(f"  fix: {_override_fix(eff) or f'git shift use {expected.name} --here'}")
        code = 1
    _print_gh(expected)
    return code


def cmd_use(args: argparse.Namespace) -> int:
    if not args.here:
        raise GitShiftError(
            "use needs --here: folders switch automatically, --here pins the current repo"
        )
    identity = store.load(args.name)
    for key, value in (("user.email", identity.email), ("user.name", identity.user_name)):
        if not value:  # a hand-edited file: git would refuse to commit, or the guard block
            raise GitShiftError(
                f'identity "{identity.name}" has no {key} in {paths.to_git_path(identity.path)}: '
                "set it there, or remove and re-add the identity"
            )
    cwd = os.getcwd()
    if resolve.repo_root(cwd) is None:
        if resolve.git_dir(cwd) is not None:
            raise GitShiftError("there is no working tree here (a bare repository, or inside .git)")
        raise GitShiftError("not inside a git repository")
    entries = [("user.name", identity.user_name), ("user.email", identity.email)]
    if identity.ssh_key:
        entries.append(("core.sshCommand", store.ssh_command(identity.ssh_key)))
    entries.append((resolve.PIN_KEY, identity.name))  # last: a pin means the rest is written
    for key, value in entries:
        gitconfig.set_value(key, value, scope="local", cwd=cwd, replace_all=True)

    identities = store.list_identities()
    eff = resolve.effective(cwd, identities)
    if resolve.same_email(eff.email, identity.email):
        print(f'{OK} this repo now commits as {identity.email} ("{identity.name}")')
    else:
        print(
            f'{WARN} pinned "{identity.name}" on this repo, but git still commits as '
            f"{_email_text(eff)} (from {_describe_source(eff)})"
        )
        fix = _override_fix(eff)
        if fix:
            print(f"  fix: {fix}")
    _print_ssh_warning(identity, eff, identities)
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "add": cmd_add,
    "list": cmd_list,
    "remove": cmd_remove,
    "status": cmd_status,
    "use": cmd_use,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-shift",
        description="Folder-based git identities, with a guard against wrong-account commits.",
    )
    parser.add_argument("--version", action="version", version=f"git-shift {__version__}")
    parser.add_argument("--debug", action="store_true", help="show tracebacks on errors")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    add = sub.add_parser("add", help="create an identity bound to a folder")
    add.add_argument("name", metavar="IDENTITY", help="identity name, e.g. perso")
    add.add_argument("--email", required=True, help="commit email")
    add.add_argument(
        "--name", dest="user_name", required=True, metavar="AUTHOR", help="author name"
    )
    add.add_argument("--folder", required=True, help="repos under this folder use the identity")
    add.add_argument("--ssh-key", help="private key used for git over SSH")
    add.add_argument("--gh", metavar="LOGIN", help="GitHub login expected for this identity")

    sub.add_parser("list", help="list identities")

    remove = sub.add_parser("remove", help="delete an identity")
    remove.add_argument("name", metavar="IDENTITY")

    sub.add_parser("status", help="show which identity applies here, and why")

    use = sub.add_parser("use", help="pin an identity on the current repository")
    use.add_argument("name", metavar="IDENTITY")
    use.add_argument("--here", action="store_true", help="write it to this repo's config")
    return parser


def _configure_output() -> None:
    """Write UTF-8 like git when output is piped or goes to mintty (Windows would use cp1252),
    and never crash on a character the stream cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if stream.isatty():
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8", errors="replace", newline="\n")


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except GitShiftError as exc:
        print(f"git-shift: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.debug:
            raise
        print(
            f"git-shift: unexpected error: {type(exc).__name__}: {exc} "
            "(rerun as: git shift --debug <command> ...)",
            file=sys.stderr,
        )
        return 1
