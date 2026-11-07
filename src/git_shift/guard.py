"""Global hooks that block commits and pushes made with the wrong identity."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from . import gitconfig, paths, store
from .errors import GitShiftError

GUARDED = ("pre-commit", "pre-push")
# Chain-only hooks: a global core.hooksPath disables every local hook type, so these
# keep repositories' own hooks working. reference-transaction and post-index-change are
# left out on purpose: they run on almost every git command.
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
    "push-to-checkout",
)
BLOCK_EXIT = 3  # distinct from Python's own exit codes (1 = crash, 2 = usage)
LAUNCHER = "_git_shift_launch.py"
STATE_PREVIOUS = "gitshift.previousHooksPath"
_PY_LINE = re.compile(r"^GIT_SHIFT_PY=(.+)$", re.MULTILINE)
_FEED = '{ [ -z "$GIT_SHIFT_STDIN" ] || printf \'%s\\n\' "$GIT_SHIFT_STDIN"; } | '


def render_launcher() -> str:
    package_parent = str(Path(__file__).resolve().parent.parent)
    return (
        "# git-shift hook launcher. Run with python -I so repository files are never imported.\n"
        "import sys\n"
        f"sys.path.insert(0, {package_parent!r})\n"
        "from git_shift.cli import main\n"
        "sys.exit(main(sys.argv[1:]))\n"
    )


def render_hook(hook: str, python: str, launcher: str, chain_dir: str | None) -> str:
    """A POSIX sh script. Paths are quoted with shlex: Git for Windows runs hooks with sh too."""
    reads_stdin = hook == "pre-push"  # stdin must reach both the check and the chained hook
    feed = _FEED if reads_stdin else ""
    lines = ["#!/bin/sh", f"# git-shift guard ({hook}). Remove with: git shift guard uninstall"]
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
            '    echo "git-shift: warning: $GIT_SHIFT_PY not found, guard skipped" >&2',
            "  fi",
            "fi",
        ]
    if chain_dir is None:
        lines.append('GIT_SHIFT_CHAIN="$(git rev-parse --git-common-dir)/hooks"')
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
    """Whether two paths name the same directory. This is a filesystem question, not
    gitdir/i matching: normcase ignores case on Windows, where paths are case-insensitive,
    and keeps it on Linux, where they are not. macOS keeps it too although its disks
    usually ignore case, which is fine: git-shift compares against the value it wrote."""
    return os.path.normcase(paths.to_git_path(a)) == os.path.normcase(paths.to_git_path(b))


def _global_hooks_path() -> str | None:
    """The global core.hooksPath git uses: the last value in its global files, read in git's
    order (XDG, then ~/.gitconfig). `git config --global` reads only one of the two files.
    The system config and [include]d files are left out."""
    values: list[str] = []
    for file in reversed(paths.global_config_files()):
        values += gitconfig.get_all("core.hooksPath", file=file)
    return values[-1] if values else None


def is_installed() -> bool:
    current = _global_hooks_path()
    return bool(current) and _same_dir(current, paths.hooks_dir())


def _saved_previous() -> str | None:
    """The hooks path saved at install, unless it is git-shift's own: chaining to it would
    make every hook run itself forever."""
    previous = gitconfig.get(STATE_PREVIOUS, file=paths.state_file())
    if previous and _same_dir(previous, paths.hooks_dir()):
        return None
    return previous


def _chain_dir(previous: str | None) -> str | None:
    if not previous:
        return None
    if previous.startswith("~") or os.path.isabs(previous):
        return paths.to_git_path(previous)
    return previous  # relative: git resolves it from each repository's top level


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


def install() -> str | None:
    """Install the hooks. Returns the previous global core.hooksPath that is chained."""
    state = paths.state_file()
    # a reinstall keeps the saved value; otherwise git's current one is never ours
    previous = _saved_previous() if is_installed() else _global_hooks_path()
    if previous:
        gitconfig.set_value(STATE_PREVIOUS, previous, file=state, replace_all=True)
    else:
        gitconfig.unset(STATE_PREVIOUS, file=state)

    hooks = paths.hooks_dir()
    launcher = hooks / LAUNCHER
    _write_script(launcher, render_launcher(), 0o644)
    # not to_git_path: resolving a virtualenv's python symlink would leave the virtualenv
    python = sys.executable.replace("\\", "/") if os.name == "nt" else sys.executable
    chain_dir = _chain_dir(previous)
    for hook in GUARDED + PASSTHROUGH:
        text = render_hook(hook, python, paths.to_git_path(launcher), chain_dir)
        _write_script(hooks / hook, text, 0o755)

    store.backup_global_config()
    # replace_all: git refuses to overwrite several values with one
    gitconfig.set_value(
        "core.hooksPath", paths.to_git_path(hooks), scope="global", replace_all=True
    )
    return previous


def uninstall() -> str | None:
    """Remove the hooks and restore the previous global core.hooksPath, if any."""
    if not is_installed():
        raise GitShiftError(
            "the guard is not installed (global core.hooksPath does not point to git-shift)"
        )
    previous = _saved_previous()
    gitconfig.unset("core.hooksPath", scope="global")
    # --global writes one of git's two global files. When the previous value is in the
    # other one, removing ours brings it back, and writing it again would duplicate it.
    # Written here, it wins: when both files exist, --global writes the one git reads last.
    current = _global_hooks_path()
    if previous and current != previous:
        gitconfig.set_value("core.hooksPath", previous, scope="global")
    elif current and _same_dir(current, paths.hooks_dir()):
        # ours is in the XDG file too (written before ~/.gitconfig existed): deleting the
        # hooks now would leave git pointing at a missing dir, so no hook would run at all
        raise GitShiftError(
            f"core.hooksPath in {paths.xdg_git_config()} also points to git-shift: "
            "remove it there to turn the guard off"
        )
    gitconfig.unset(STATE_PREVIOUS, file=paths.state_file())
    shutil.rmtree(paths.hooks_dir(), ignore_errors=True)
    return previous


def installed_python() -> str | None:
    script = paths.hooks_dir() / "pre-commit"
    if not script.is_file():
        return None
    match = _PY_LINE.search(script.read_text(encoding="utf-8"))
    return shlex.split(match.group(1))[0] if match else None
