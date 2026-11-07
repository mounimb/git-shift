"""Global hooks that block commits and pushes made with the wrong identity."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import gitconfig, paths, resolve, store
from .errors import GitShiftError

GUARDED = ("pre-commit", "pre-push")
# Chain-only hooks: a global core.hooksPath disables every local hook type, so these keep
# repositories' own hooks working, on both sides of a push. Left out on purpose:
# - reference-transaction and post-index-change: they run on almost every git command;
# - update: it runs once per pushed ref, so a local push of 300 tags took 44 s instead
#   of 1 s on Windows;
# - post-update: it gets every pushed ref on its command line, which Windows cannot start
#   for a few thousand refs, so git prints an error;
# - push-to-checkout: just by existing, it replaces the worktree update that
#   receive.denyCurrentBranch=updateInstead does;
# - proc-receive: only code review servers use it (receive.procReceiveRefs), and it talks
#   a protocol with receive-pack;
# - the git-p4 hooks (p4-changelist, p4-prepare-changelist, p4-post-changelist,
#   p4-pre-submit): only the Perforce bridge runs them.
PASSTHROUGH = (
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-auto-gc",
    "post-rewrite",
    "sendemail-validate",
    "pre-receive",
    "post-receive",
)
BLOCK_EXIT = 3  # distinct from Python's own exit codes (1 = crash, 2 = usage)
MAX_DEPTH = 5  # git-shift hooks running inside each other: more means core.hooksPath loops
LAUNCHER = "_git_shift_launch.py"
STATE_PREVIOUS = "gitshift.previousHooksPath"
_MARKER = "# git-shift guard ("  # starts the second line of every generated hook
_PY_LINE = re.compile(r"^GIT_SHIFT_PY=(.+)$", re.MULTILINE)
_FEED = '{ [ -z "$GIT_SHIFT_STDIN" ] || printf \'%s\\n\' "$GIT_SHIFT_STDIN"; } | '
# First thing every hook runs. The case resets a value that is not a plain decimal number
# (with a leading 0, "08" reads as octal and fails): the arithmetic would stop the hook.
_LOOP_GUARD = [
    "case $GIT_SHIFT_DEPTH in ''|*[!0-9]*|0?*) GIT_SHIFT_DEPTH=0 ;; esac",
    "GIT_SHIFT_DEPTH=$((GIT_SHIFT_DEPTH + 1)); export GIT_SHIFT_DEPTH",
    f'if [ "$GIT_SHIFT_DEPTH" -gt {MAX_DEPTH} ]; then',
    "  printf '%s\\n' \"git-shift: warning: core.hooksPath loops back to git-shift, "
    'hooks skipped" >&2',
    "  exit 0",
    "fi",
]
_SKIPPED = (
    '"git-shift: warning: $GIT_SHIFT_PY or $GIT_SHIFT_LAUNCH missing, guard skipped '
    '(fix: git shift guard install)"'
)
_GONE = (
    "git-shift: warning: git-shift is no longer installed for this Python; "
    "remove the guard with: git config --global --unset core.hooksPath"
)
_ERE_SPECIAL = re.compile(r"[\\.^$|?*+()\[\]{}]")
_IDENT_EMAIL = re.compile(r"<([^<>]*)>")  # the first <...>: names never contain < or >
_SKIPPED_AFTER_ERROR = "git-shift: warning: guard skipped after an error:"


def render_launcher(package_parent: str | None = None) -> str:
    """The script the hooks run. package_parent: where git_shift is (default: this one)."""
    if package_parent is None:
        package_parent = str(Path(__file__).resolve().parent.parent)
    return (
        "# git-shift hook launcher. Run with python -I so repository files are never imported.\n"
        "import sys\n"
        f"sys.path.insert(0, {package_parent!r})\n"
        "try:\n"
        "    from git_shift.cli import main\n"
        "except ImportError:  # uninstalled since: never block every commit on the machine\n"
        f"    print({_GONE!r}, file=sys.stderr)\n"
        "    sys.exit(0)\n"
        "sys.exit(main(sys.argv[1:]))\n"
    )


def render_hook(hook: str, python: str, launcher: str, chain_dir: str | None) -> str:
    """A POSIX sh script. Paths are quoted with shlex: Git for Windows runs hooks with sh too.
    chain_dir None: chain to the repository's own hooks."""
    reads_stdin = hook == "pre-push"  # stdin must reach both the check and the chained hook
    feed = _FEED if reads_stdin else ""
    lines = [
        "#!/bin/sh",
        f"{_MARKER}{hook}). Remove with: git shift guard uninstall",
        *_LOOP_GUARD,
    ]
    if reads_stdin:
        lines.append("GIT_SHIFT_STDIN=$(cat)")
    if hook in GUARDED:
        lines += [
            f"GIT_SHIFT_PY={shlex.quote(python)}",
            f"GIT_SHIFT_LAUNCH={shlex.quote(launcher)}",
            'if [ -z "$GIT_SHIFT_SKIP" ]; then',
            '  if [ -f "$GIT_SHIFT_PY" ] && [ -f "$GIT_SHIFT_LAUNCH" ]; then',
            f'    {feed}"$GIT_SHIFT_PY" -I "$GIT_SHIFT_LAUNCH" __hook {hook} "$@"',
            f"    if [ $? -eq {BLOCK_EXIT} ]; then exit 1; fi",
            "  else",
            f"    printf '%s\\n' {_SKIPPED} >&2",
            "  fi",
            "fi",
        ]
    if chain_dir is None:
        # .git/hooks without starting git, except where .git is a file (linked worktrees,
        # submodules) or GIT_DIR is set (--git-dir, worktrees, receive hooks, bare repos)
        lines += [
            'if [ -z "$GIT_DIR$GIT_COMMON_DIR" ] && [ -d .git ]; then',
            "  GIT_SHIFT_CHAIN=.git/hooks",
            "else",
            '  GIT_SHIFT_CHAIN="$(git rev-parse --git-common-dir)/hooks"',
            "fi",
        ]
    else:
        lines.append(f"GIT_SHIFT_CHAIN={shlex.quote(chain_dir)}")
    lines.append(f'if [ -x "$GIT_SHIFT_CHAIN/{hook}" ]; then')
    if reads_stdin:
        lines += [f'  {feed}"$GIT_SHIFT_CHAIN/{hook}" "$@"', "  exit $?"]
    else:
        lines.append(f'  exec "$GIT_SHIFT_CHAIN/{hook}" "$@"')
    lines += ["fi", "exit 0", ""]
    return "\n".join(lines)


def _same_dir(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Whether two paths name the same directory. When both exist, the filesystem decides
    (case, links, short names). Otherwise the text: normcase ignores case on Windows, where
    paths are case-insensitive, and keeps it elsewhere. Not gitdir/i, which folds only A-Z."""
    a, b = paths.to_git_path(a), paths.to_git_path(b)
    if os.path.exists(a) and os.path.exists(b):
        return os.path.samefile(a, b)
    return os.path.normcase(a) == os.path.normcase(b)


def _global_files() -> list[str]:
    """git's global config files in the order git reads them: XDG, then ~/.gitconfig (or
    only GIT_CONFIG_GLOBAL). `git config --global` reads and writes only one of them."""
    return [paths.to_git_path(file) for file in reversed(paths.global_config_files())]


# What tells git which repository it is in: a read without them finds none
_REPOSITORY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _hooks_paths(*, scope: str | None = None, file: str | None = None) -> list[gitconfig.Origin]:
    """Every core.hooksPath in a config file (or scope) and the files it includes, read
    outside any repository. git checks [includeIf] gitdir and onbranch against the current
    repository, so from inside a work repository a work-only hooksPath would pass for the
    global one. Read from a new empty dir whose parent is a ceiling, those conditions are
    false and plain [include] still applies. realpath: a Windows short name in the temp
    path would not match the ceiling."""
    neutral = os.path.realpath(tempfile.mkdtemp(prefix="git-shift-"))
    env: dict[str, str | None] = dict.fromkeys(_REPOSITORY_ENV)
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(neutral)
    try:
        return gitconfig.get_all_with_origin(
            "core.hooksPath", scope=scope, file=file, includes=True, cwd=neutral, env=env
        )
    finally:
        shutil.rmtree(neutral, ignore_errors=True)


def _global_entries() -> list[gitconfig.Origin]:
    """Every global core.hooksPath, in git's read order, with the files they include, as
    seen outside any repository. The system config is read apart."""
    entries: list[gitconfig.Origin] = []
    for file in _global_files():
        entries += _hooks_paths(file=file)
    return entries


def _global_hooks_path() -> str | None:
    """The global core.hooksPath git uses: the last one it reads."""
    entries = _global_entries()
    return entries[-1].value if entries else None


def _system_hooks_path() -> str | None:
    """The system core.hooksPath git uses outside any repository. `git config --system`
    reads it even under GIT_CONFIG_NOSYSTEM, which git itself honours."""
    nosystem = os.environ.get("GIT_CONFIG_NOSYSTEM", "").strip().lower()
    if nosystem not in ("", "0", "false", "no", "off"):
        return None
    entries = _hooks_paths(scope="system")
    return entries[-1].value if entries else None


def is_installed() -> bool:
    """Whether git uses the guard: the last global core.hooksPath is git-shift's."""
    current = _global_hooks_path()
    return bool(current) and _same_dir(current, paths.hooks_dir())


def _chain_dir(value: str | None) -> str | None:
    if not value:
        return None
    expanded = paths.expand(value)  # ~ and ~/ use git's home
    if expanded.startswith("~"):
        expanded = os.path.expanduser(expanded)  # ~user/: that user's home
    # a Windows "/c/..." value stays as Python reads it: the runtime loop guard covers it
    if os.path.isabs(expanded):
        return paths.to_git_path(expanded)
    return value  # relative: git resolves it from each repository's top level


def _chainable(value: str) -> bool:
    """Whether the hooks may chain to this hooks path: never to git-shift's own dir, nor to
    another guard's (installed with another config dir), where the hooks would loop."""
    directory = _chain_dir(value)
    if not directory or not os.path.isabs(directory):
        return True  # one dir per repository: the runtime loop guard covers it
    if _same_dir(directory, paths.hooks_dir()):
        return False
    try:
        return _MARKER.encode() not in (Path(directory) / "pre-commit").read_bytes()
    except OSError:
        return True  # no pre-commit there


def _saved_previous() -> str | None:
    """The global core.hooksPath saved at install, if it can still be chained."""
    previous = gitconfig.get(STATE_PREVIOUS, file=paths.state_file())
    return previous if previous and _chainable(previous) else None


def _exactly(values: set[str]) -> str:
    """A POSIX extended regex that matches only these values, for git config."""
    escaped = (_ERE_SPECIAL.sub(lambda match: "\\" + match.group(), v) for v in sorted(values))
    return "^(" + "|".join(escaped) + ")$"


def _write_script(path: Path, text: str, mode: int) -> None:
    """Write in one step, mode included: git can run a hook while a reinstall replaces it,
    and a partial script, or one without its exec bit, would fail or be skipped."""
    tmp = store.temp_beside(path)
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.chmod(mode)
        store.replace_file(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Installed:
    """What install() set up."""

    chain: str | None  # the hooks path run after the guard; None: each repository's own
    chain_is_system: bool  # from the system config, which uninstall leaves alone
    overridden_by: gitconfig.Origin | None  # the core.hooksPath git uses instead, if any


def install() -> Installed:
    """Install the hooks, chained to the previous global core.hooksPath, else to the
    system one."""
    hooks = paths.hooks_dir()
    state = paths.state_file()
    previous = _saved_previous() if is_installed() else _global_hooks_path()
    if previous and not _chainable(previous):
        previous = None
    if previous:
        gitconfig.set_value(STATE_PREVIOUS, previous, file=state, replace_all=True)
    else:
        gitconfig.unset(STATE_PREVIOUS, file=state)
    # not saved: removing ours brings the system value back by itself
    system = None if previous else _system_hooks_path()
    if system and not _chainable(system):
        system = None
    chain = previous or system

    launcher = hooks / LAUNCHER
    _write_script(launcher, render_launcher(), 0o644)
    # not to_git_path: resolving a virtualenv's python symlink would leave the virtualenv
    python = sys.executable.replace("\\", "/") if os.name == "nt" else sys.executable
    chain_dir = _chain_dir(chain)
    for hook in GUARDED + PASSTHROUGH:
        text = render_hook(hook, python, paths.to_git_path(launcher), chain_dir)
        _write_script(hooks / hook, text, 0o755)

    store.backup_global_config()
    # replace_all: git refuses to overwrite several values with one
    gitconfig.set_value(
        "core.hooksPath", paths.to_git_path(hooks), scope="global", replace_all=True
    )
    entries = _global_entries()  # an [include] that git reads after ours still wins
    current = entries[-1] if entries else None
    overridden = current if current and not _same_dir(current.value, hooks) else None
    return Installed(chain=chain, chain_is_system=bool(system), overridden_by=overridden)


def uninstall() -> str | None:
    """Remove the hooks and restore the previous global core.hooksPath. Returns it."""
    hooks = paths.hooks_dir()
    files = _global_files()
    entries = _global_entries()
    ours = [e for e in entries if e.path in files and _same_dir(e.value, hooks)]
    if not ours:  # also when an [include] overrides ours (install warns): it can still go
        raise GitShiftError(
            "the guard is not installed (global core.hooksPath does not point to git-shift)"
        )
    previous = _saved_previous()
    target = paths.to_git_path(store.global_config_file() or paths.global_config_files()[0])
    # Decided before writing: the file --global writes changes in one step, and git reads
    # it last, so an interruption never leaves the user's value lost. Ours in the other
    # global file (written before ~/.gitconfig existed) is removed after.
    left = [e.value for e in entries if e not in ours]
    ours_in_target = {e.value for e in ours if e.path == target}
    if previous and (left[-1] if left else None) != previous:
        if ours_in_target:
            gitconfig.set_value(
                "core.hooksPath",
                previous,
                scope="global",
                replace_all=True,
                value_regex=_exactly(ours_in_target),
            )
        else:
            gitconfig.add_value("core.hooksPath", previous, scope="global")
    elif ours_in_target:
        gitconfig.unset("core.hooksPath", scope="global", value_regex=_exactly(ours_in_target))
    for file in files:
        ours_here = {e.value for e in ours if e.path == file}
        if file != target and ours_here:
            gitconfig.unset("core.hooksPath", file=file, value_regex=_exactly(ours_here))
    gitconfig.unset(STATE_PREVIOUS, file=paths.state_file())
    shutil.rmtree(hooks, ignore_errors=True)
    return previous


def installed_python() -> str | None:
    script = paths.hooks_dir() / "pre-commit"
    if not script.is_file():
        return None
    match = _PY_LINE.search(script.read_text(encoding="utf-8"))
    return shlex.split(match.group(1))[0] if match else None


# the checks the hooks run


def author_email(cwd: str | os.PathLike[str]) -> str | None:
    """The author email git will record, or None when git has none.

    Inside `git commit`, determine_author_info exports the final author email, cleaned as
    git records it, as GIT_AUTHOR_EMAIL before pre-commit runs: the old author on --amend,
    the value of --author, else the configured one. Reading it saves a git process.
    Elsewhere (`git hook run`, a direct call) `git var` works it out from GIT_AUTHOR_*,
    -c options and the config."""
    exported = os.environ.get("GIT_AUTHOR_EMAIL")
    if exported:
        return exported
    proc = gitconfig.run_git("var", "GIT_AUTHOR_IDENT", cwd=cwd, check=False)
    match = _IDENT_EMAIL.search(proc.stdout) if proc.returncode == 0 else None
    return match.group(1) if match else None


def check_pre_commit(cwd: str | os.PathLike[str]) -> int:
    """BLOCK_EXIT, with the reason on stderr, when the author email is not the one of the
    identity the repository expects (its pin, its folder, then the GitHub owner of origin)."""
    identities = store.list_identities()
    if not identities:
        return 0  # nothing can match: skip the git calls of expected_for_repo
    found = resolve.expected_for_repo(cwd, identities)
    if found is None:
        return 0
    identity, reason = found
    actual = author_email(cwd)
    if resolve.same_email(actual, identity.email):
        return 0
    owner = f" ({identity.gh})" if identity.gh else ""
    lines = [  # a literal ✗: importing cli for ERR would be circular
        f'✗ git-shift: this repo belongs to "{identity.name}"{owner}, '
        f"but you are committing as {actual or '(no email)'}",
        f"  expected: {identity.email}  (matched by {reason})",
        f"  fix:      git shift use {identity.name} --here",
        "  why:      git shift status shows where the wrong email comes from",
        "  amending? add --reset-author; using --author? drop it",
        "  bypass:   git commit --no-verify",
    ]
    print("\n".join(lines), file=sys.stderr)
    return BLOCK_EXIT


def check_pre_push(cwd: str | os.PathLike[str], args: list[str], stdin_text: str) -> int:
    return 0  # implemented in Task 15


def _error_lines(exc: Exception) -> list[str]:
    """The error, indented line by line: what git says when it refuses can span several
    lines. Anything but a GitShiftError is a bug, so its type is shown for the report."""
    message = str(exc).strip()
    if not isinstance(exc, GitShiftError) or not message:
        message = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return [f"  {line}" if line.strip() else "" for line in message.splitlines()]


def run_hook(hook: str, args: list[str], stdin: TextIO) -> int:
    """What the generated hooks run: BLOCK_EXIT blocks, anything else lets git go on.
    Fails open: an error in git-shift must never block every commit on the machine."""
    try:
        if hook == "pre-commit":
            return check_pre_commit(os.getcwd())
        if hook == "pre-push":
            return check_pre_push(os.getcwd(), args, stdin.read())
        return 0
    except Exception as exc:
        print("\n".join([_SKIPPED_AFTER_ERROR, *_error_lines(exc)]), file=sys.stderr)
        return 0
