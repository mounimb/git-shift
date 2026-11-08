"""Command-line interface for git-shift."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from . import __version__, doctor, explain, gh, gitconfig, guard, paths, resolve, store
from .errors import GitShiftError

OK, WARN, ERR = "✓", "!", "✗"


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


def _print_problem(symbol: str, problem: str | None, fix: str | None) -> None:
    if problem:
        print(f"{symbol} {problem}")
    if fix:
        print(f"  fix: {fix}")


def _print_gh(expected: store.Identity | None) -> None:
    """The gh account against the one the identity expects. Advice only: never changes
    the exit code."""
    if expected is None or not expected.gh:
        return
    accounts = gh.accounts()
    if accounts is None:
        return
    login = expected.gh
    active = gh.active_login(accounts, explain.GH_HOST)
    if active is not None and active.casefold() == login.casefold():
        print(f"gh:       {active} (active)")
        return
    print(f"gh:       active account is {active or 'none'}, expected {login}")
    _print_problem(WARN, *explain.gh_mismatch(login, accounts))


def _print_ssh_warning(
    identity: store.Identity, eff: resolve.Effective, identities: list[store.Identity]
) -> None:
    _print_problem(
        WARN, explain.ssh_problem(identity, eff), explain.ssh_fix(identity, eff, identities)
    )


def _repo_text(cwd: str, root: str | None) -> str:
    if root:
        return root
    directory = resolve.git_dir(cwd)  # a bare repository, or inside .git
    return f"{directory} (no working tree)" if directory else "(not a git repository)"


def cmd_status(args: argparse.Namespace) -> int:
    cwd = os.getcwd()
    identities = store.list_identities()
    root = resolve.repo_root(cwd)
    match = resolve.match_for_repo(cwd, identities)
    expected = match.identity if match else None
    print(f"repo:     {_repo_text(cwd, root)}")
    if match is None:
        print("expected: no identity matches this location")
    else:
        print(f'expected: "{expected.name}" <{expected.email}>  (matched by {match.reason})')
    if root is None:
        return 0
    pin = explain.dangling_pin(cwd, identities, match)
    if pin:
        _print_problem(WARN, *pin)
    eff = resolve.effective(cwd, identities)
    if eff.name:
        print(f"name:     {eff.name}")
    print(f"email:    {explain.email_text(eff)}  (from {explain.describe_source(eff)})")
    if eff.ssh_command:
        print(f"ssh:      {eff.ssh_command}")
    code = 0
    if match is not None and not resolve.same_email(eff.email, expected.email):
        problem = explain.mismatch(eff, expected, match.kind)
        _print_problem(ERR, problem, explain.mismatch_fix(eff, expected))
        code = 1
    if expected is not None and explain.pushes_use_the_wrong_key(expected, eff, identities):
        _print_ssh_warning(expected, eff, identities)  # advice only, like the gh lines
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
            f"{explain.email_text(eff)} (from {explain.describe_source(eff)})"
        )
        _print_problem(WARN, None, explain.override_fix(eff))
    # stricter than status: a pin should leave SSH as the identity wants it
    wanted = store.ssh_command(identity.ssh_key) if identity.ssh_key else explain.PLAIN_SSH
    if eff.ssh_command and eff.ssh_command != wanted:
        _print_ssh_warning(identity, eff, identities)
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    if args.guard_command == "install":
        installed = guard.install()
        override = installed.overridden_by
        if override is None:
            print(f"{OK} guard installed: commits and pushes with the wrong identity are blocked")
        else:
            print(
                f"{WARN} guard installed, but not active: git uses core.hooksPath "
                f"{override.value} from {override.path}, which it reads after git-shift's"
            )
            print(f"  to make it active: remove core.hooksPath from {override.path}")
        if installed.chain_is_system:
            print(f"  chained to the system hooks: {installed.chain}")
        elif installed.chain:
            print(f"  chained to your previous global hooks: {installed.chain}")
        else:
            print("  chained to each repository's own hooks")
        print("  bypass once: git commit --no-verify   disable: git shift guard uninstall")
        return 0
    previous = guard.uninstall()
    print(f"{OK} guard removed" + (f", core.hooksPath restored to {previous}" if previous else ""))
    return 0


_SYMBOLS = {doctor.OK: OK, doctor.WARN: WARN, doctor.ERROR: ERR}


def _print_finding(finding: doctor.Finding) -> None:
    first, *rest = finding.message.split("\n")  # git's own messages can span several lines
    print(f"{_SYMBOLS[finding.level]} {first}")
    for line in rest:
        print(f"  {line}" if line.strip() else "")
    if finding.fix:
        print(f"  fix: {finding.fix}")


def cmd_doctor(args: argparse.Namespace) -> int:
    findings = doctor.run(os.getcwd())
    for finding in findings:
        _print_finding(finding)
    return 1 if any(finding.level == doctor.ERROR for finding in findings) else 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "add": cmd_add,
    "list": cmd_list,
    "remove": cmd_remove,
    "status": cmd_status,
    "use": cmd_use,
    "doctor": cmd_doctor,
    "guard": cmd_guard,
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

    sub.add_parser("doctor", help="check your setup and say how to fix it")

    guard_parser = sub.add_parser("guard", help="block commits made with the wrong identity")
    guard_sub = guard_parser.add_subparsers(dest="guard_command", required=True, metavar="<action>")
    guard_sub.add_parser("install", help="install the global hooks")
    guard_sub.add_parser("uninstall", help="remove the global hooks")
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
    if argv[:1] == ["__hook"]:  # run by the generated hooks: no user command, not in -h
        return guard.run_hook(argv[1] if len(argv) > 1 else "", argv[2:], sys.stdin)
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
