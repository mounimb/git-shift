"""Command-line interface for git-shift."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from . import __version__, store
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
        print("  git shift add <name> --email <email> --name <name> --folder <dir>")
        return 0
    width = max(len(identity.name) for identity in identities)
    for identity in identities:
        folder = identity.folder or "(no usable folder)"  # hand-edited file, see doctor
        gh_part = f"  gh: {identity.gh}" if identity.gh else ""
        print(f"{identity.name:<{width}}  {identity.email}  {folder}{gh_part}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    identity = store.remove_identity(args.name)
    print(f'{OK} identity "{identity.name}" removed')
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "add": cmd_add,
    "list": cmd_list,
    "remove": cmd_remove,
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
    add.add_argument("name", help="identity name, e.g. perso")
    add.add_argument("--email", required=True, help="commit email")
    add.add_argument("--name", dest="user_name", required=True, metavar="NAME", help="author name")
    add.add_argument("--folder", required=True, help="repos under this folder use the identity")
    add.add_argument("--ssh-key", help="private key used for git over SSH")
    add.add_argument("--gh", metavar="LOGIN", help="GitHub login expected for this identity")

    sub.add_parser("list", help="list identities")

    remove = sub.add_parser("remove", help="delete an identity")
    remove.add_argument("name")
    return parser


def _configure_output() -> None:
    """Never crash on a console that cannot print symbols like ✓."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except GitShiftError as exc:
        print(f"git-shift: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        if args.debug:
            raise
        print(f"git-shift: unexpected error: {exc} (rerun with --debug)", file=sys.stderr)
        return 1
