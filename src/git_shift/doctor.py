"""Health checks behind `git shift doctor`: each finding says what is wrong and how to fix it.

doctor only reads. `--fix` rewrites rules.gitconfig, a file git-shift fully owns, from the
identity files, and nothing else: the other fixes are printed for the user to run.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import explain, gh, gitconfig, guard, paths, remotes, resolve, store
from .errors import GitCommandError, GitShiftError

OK, WARN, ERROR = "ok", "warn", "error"
MIN_GIT = (2, 13)  # includeIf
FIX_RULES = "git shift doctor --fix"
OUT_OF_DATE = "rules.gitconfig is out of date: it does not match your identity files"
ADD = 'git shift add <identity> --email <email> --name "<your name>" --folder <dir>'
GUARD_INSTALL = "git shift guard install"
# global keys git uses instead of an identity's: author.* is what commits record and
# what the guard checks, committer.* only the committer line
_OVERRIDES = {
    "author.email": (ERROR, "which git uses instead of every identity's email"),
    "author.name": (ERROR, "which git uses instead of every identity's name"),
    "committer.email": (WARN, "which git records as the committer instead of the identity's"),
    "committer.name": (WARN, "which git records as the committer instead of the identity's"),
}


@dataclass(frozen=True)
class Finding:
    level: str  # OK | WARN | ERROR
    message: str  # may span several lines, as when it quotes git
    fix: str | None = None


def run(cwd: str | os.PathLike[str]) -> list[Finding]:
    """Every check, in order. A check that git makes fail becomes an error finding, and the
    other checks still run. Only the repository checks run from cwd: the others read the
    global config and git-shift's files from outside any repository, so a broken
    .git/config here is reported once, by the repository check."""
    with gitconfig.outside_repositories():
        try:
            version = gitconfig.git_version()
        except GitCommandError as exc:  # even git --version reads the config: all git fails
            message = f"your git config cannot be read: {_git_said(exc)}"
            return [Finding(ERROR, message, "correct the line git names")]
        except GitShiftError as exc:
            return [Finding(ERROR, str(exc))]  # without git nothing else can be checked
        findings = [_check_git(version)]
        identities, unreadable = load_identities()
        have_files = bool(identities or unreadable)
        findings += unreadable
        findings += _check_identities(identities, have_files)
        if have_files:
            findings += _guarded("the git-shift include", _check_include)
            findings += _guarded("the global author keys", _check_overrides)
        if not unreadable:  # the rules of an unreadable identity are unknown
            findings += _guarded("rules.gitconfig", lambda: _check_rules(identities))
        findings += _guarded("gh", lambda: _check_gh(identities))
        findings += _guarded("the guard", _check_guard)
    findings += _guarded("this repository", lambda: _check_repo(cwd, identities))
    return findings


def fix_rules() -> Finding:
    """Rewrite rules.gitconfig from the identity files, when they all load."""
    with gitconfig.outside_repositories():
        return _fix_rules()


def _fix_rules() -> Finding:
    identities, unreadable = load_identities()
    if unreadable:
        return Finding(ERROR, "rules.gitconfig not rewritten: an identity file cannot be read")
    count = _count(identities)
    try:
        fresh = paths.rules_file().is_file() and rules_up_to_date(identities)
    except GitShiftError:
        fresh = False  # malformed: rewriting it is the fix
    if fresh:
        return Finding(OK, f"rules.gitconfig already matches your {count}: nothing to rewrite")
    try:
        store.write_rules(identities)
    except (GitShiftError, OSError) as exc:  # permissions, a locked file: the checks still run
        said = _git_said(exc) if isinstance(exc, GitShiftError) else exc.strerror or str(exc)
        return Finding(ERROR, f"could not rewrite rules.gitconfig: {said}")
    return Finding(OK, f"rewrote rules.gitconfig from your {count}")


def load_identities() -> tuple[list[store.Identity], list[Finding]]:
    """The identities that load, and an error for the others. One at a time, so the good
    ones still count. Names failing with the same message share one finding: from inside a
    repository with a malformed .git/config, every git config call fails."""
    identities = []
    failed: dict[str, list[str]] = {}
    for name in store.identity_names():
        try:
            identities.append(store.load(name))
        except GitShiftError as exc:
            failed.setdefault(_git_said(exc), []).append(name)
    findings = []
    for said, names in failed.items():
        noun = "identity" if len(names) == 1 else "identities"
        quoted = ", ".join(f'"{name}"' for name in names)
        findings.append(
            Finding(ERROR, f"cannot read {noun} {quoted}: {said}", "correct the line git names")
        )
    return identities, findings


def rules_up_to_date(identities: list[store.Identity]) -> bool:
    """Whether rules.gitconfig has exactly the entries write_rules would write, in the same
    order, compared as git reads them: patterns ignoring A-Z case like gitdir/i, targets as
    the files they name. A pattern that git compares differently counts: a folder added
    before it existed can resolve to another spelling once it does (~/É, then ~/é on
    Windows), or through a symlink."""
    rules = paths.rules_file()
    if not rules.is_file():
        return not identities
    actual = [_comparable(key, value) for key, value in gitconfig.list_entries(file=rules)]
    expected = [_comparable(key, value) for key, value in store.rule_entries(identities)]
    return actual == expected


def _comparable(key: str, value: str) -> tuple[str, str]:
    """A rules entry as git reads it. git config --list lowercases the section and the name
    but keeps the subsection, the includeIf condition. git matches the condition's keyword
    exactly (GITDIR/I: never applies), and gitdir/i ignores A-Z case in the pattern only."""
    section, _, rest = key.partition(".")
    condition, _, name = rest.rpartition(".")
    keyword, colon, pattern = condition.partition(":")
    if keyword.endswith("/i"):
        pattern = paths.fold_ascii(pattern)
    target = paths.expand(value)
    if not os.path.isabs(target):  # git resolves it from the file that includes it
        target = os.path.join(paths.rules_file().parent, target)
    folded = f"{section.lower()}.{keyword}{colon}{pattern}.{name.lower()}"
    return folded, os.path.normcase(paths.to_git_path(target))


def _git_said(exc: GitShiftError) -> str:
    """What git said, which names the file at fault, without the command we ran."""
    if isinstance(exc, GitCommandError) and exc.stderr:
        return exc.stderr
    return str(exc)


def _guarded(what: str, check: Callable[[], list[Finding]]) -> list[Finding]:
    try:
        return check()
    except (GitShiftError, OSError) as exc:
        said = _git_said(exc) if isinstance(exc, GitShiftError) else str(exc)
        return [Finding(ERROR, f"could not check {what}: {said}")]


def _count(identities: list[store.Identity]) -> str:
    return f"{len(identities)} identit{'y' if len(identities) == 1 else 'ies'}"


def _check_git(version: tuple[int, int]) -> Finding:
    major, minor = version
    if version < MIN_GIT:
        needed = ".".join(map(str, MIN_GIT))
        return Finding(
            ERROR,
            f"git {major}.{minor} is too old: git-shift needs git {needed} or later (includeIf)",
            "update git",
        )
    return Finding(OK, f"git {major}.{minor}")


def _check_identities(identities: list[store.Identity], have_files: bool) -> list[Finding]:
    if not have_files:
        return [Finding(WARN, "no identities yet", ADD)]
    findings = []
    for identity in identities:
        findings += _check_identity(identity)
    for inner, outer in store.inherited_ssh_keys(identities):
        findings.append(
            Finding(
                WARN,
                store.inherited_ssh_key_text(inner, outer),
                f'if "{inner.name}" needs its own key: git shift remove {inner.name}, '
                "then add it again with --ssh-key",
            )
        )
    if identities and not findings:
        names = ", ".join(identity.name for identity in identities)
        findings.append(Finding(OK, f"{_count(identities)}: {names}"))
    return findings


def _check_identity(identity: store.Identity) -> list[Finding]:
    name = identity.name
    set_it = f'git config --file "{paths.to_git_path(identity.path)}"'
    findings = []
    if not identity.email:  # resolve ignores it
        findings.append(
            Finding(
                WARN,
                f'identity "{name}" has no email, so it applies to no repository',
                f"{set_it} user.email <email>",
            )
        )
    if not identity.user_name:
        findings.append(
            Finding(
                WARN,
                f'identity "{name}" has no author name, so git takes the name from elsewhere '
                "or refuses to commit",
                f'{set_it} user.name "<your name>"',
            )
        )
    if not identity.folder:  # missing or relative: write_rules writes no rule for it
        findings.append(
            Finding(
                WARN,
                f'identity "{name}" has no usable folder, so it applies to no repository',
                f'{set_it} gitshift.folder "<folder>", then: {FIX_RULES}',
            )
        )
    elif not Path(identity.folder).is_dir():
        findings.append(
            Finding(WARN, f'the folder of "{name}" does not exist yet: {identity.folder}')
        )
    key = identity.ssh_key
    if key and not Path(paths.expand(key)).is_file():
        # expanded: ssh-keygen gets no ~ inside double quotes (a hand-edited ~/...)
        findings.append(
            Finding(
                ERROR,
                f'the SSH key of "{name}" is missing: {key}',
                f'ssh-keygen -t ed25519 -f "{paths.to_git_path(key)}"',
            )
        )
    return findings


def _check_include() -> list[Finding]:
    late = store.keys_after_global_include()
    rules = paths.to_git_path(paths.rules_file())
    first = store.global_config_file()  # the file `git config --global` reads and writes
    where = paths.to_git_path(first) if first else "your global git config"
    if late is None:  # missing, or only in the XDG file, which git reads before ~/.gitconfig
        if first is None or store.include_lands_last(first):
            fix = f'git config --global --add include.path "{rules}"'
        else:  # --add would put it in an [include] section that other settings follow
            fix = f"add this line at the end of {where}: {store.include_line()}"
        message = f"{where} does not include rules.gitconfig, so your identities may not apply"
        return [Finding(ERROR, message, fix)]
    if late:
        return [
            Finding(
                ERROR,
                f"your global config sets {', '.join(late)} after the git-shift include, "
                "so it can override your identities",
                f"in {where}, move path = {rules} to an [include] section at the end, below "
                "those keys and any [include] or [includeIf] that sets them",
            )
        ]
    return [Finding(OK, f"{where} includes rules.gitconfig, and nothing after it overrides it")]


def _check_overrides() -> list[Finding]:
    """author.* and committer.* in either global file: git reads both, while
    store.global_author_overrides() reads only the one --global names."""
    findings = []
    for file in paths.global_config_files():
        entries = gitconfig.list_entries(file=file)  # [] when the file does not exist
        where = paths.to_git_path(file)
        for key in sorted({key for key, _ in entries if key in _OVERRIDES}):
            level, effect = _OVERRIDES[key]
            fix = explain.unset_global(key, where)
            findings.append(Finding(level, f"{where} sets {key}, {effect}", fix))
    return findings


def _check_rules(identities: list[store.Identity]) -> list[Finding]:
    if not identities and not paths.rules_file().exists():
        return []
    try:
        fresh = rules_up_to_date(identities)
    except GitShiftError as exc:  # malformed: every git command reading it fails
        return [Finding(ERROR, f"rules.gitconfig cannot be read: {_git_said(exc)}", FIX_RULES)]
    if not fresh:
        return [Finding(ERROR, OUT_OF_DATE, FIX_RULES)]
    return [Finding(OK, "rules.gitconfig matches your identity files")]


def _check_gh(identities: list[store.Identity]) -> list[Finding]:
    accounts = gh.accounts()
    if accounts is None:
        return [Finding(OK, "gh not installed or not responding: GitHub CLI checks skipped")]
    expected = {identity.gh.casefold() for identity in identities if identity.gh}
    findings = []
    tokens = explain.gh_token_variables()  # presence only, never the values
    if tokens and expected:
        problem = explain.gh_token_problem(tokens)
        findings.append(Finding(WARN, problem, explain.unset_variables(tokens)))
    # offline, gh reports every login as invalid: one warning, not one per login
    hosts = list(dict.fromkeys(account.host for account in accounts))
    unverified = [h for h in hosts if not any(a.valid for a in accounts if a.host == h)]
    for host in unverified:
        findings.append(
            Finding(
                WARN,
                f"could not verify the gh logins on {host} (offline?): "
                "gh says every one is invalid",
                f"if you are online: gh auth login -h {host}",
            )
        )
    for account in accounts:
        if account.valid or account.host in unverified:
            continue
        if account.host == explain.GH_HOST and account.login.casefold() in expected:
            continue  # reported below, with the identity that expects it
        host, login = account.host, account.login
        findings.append(
            Finding(
                WARN,
                f"the gh login of {login} on {host} is invalid or expired",
                f"gh auth login -h {host}   (or forget it: gh auth logout -h {host} -u {login})",
            )
        )
    for identity in identities:
        if not identity.gh:
            continue
        known = any(
            a.host == explain.GH_HOST and a.login.casefold() == identity.gh.casefold()
            for a in accounts
        )
        if known and explain.GH_HOST in unverified:
            continue  # the offline warning covers it
        problem = explain.gh_login_problem(identity.gh, accounts)
        if problem:  # a warning: gh is optional
            text, fix = problem
            findings.append(Finding(WARN, f'{text}, which identity "{identity.name}" expects', fix))
    return findings or [Finding(OK, "gh logins are valid")]


def _check_guard() -> list[Finding]:
    override = guard.overridden_by()
    findings = []
    if override is not None:
        findings.append(
            Finding(
                ERROR,
                f"the guard is installed but not active: git uses core.hooksPath "
                f"{override.value} from {override.path}, which it reads after git-shift's",
                f"remove core.hooksPath from {override.path}",
            )
        )
    elif not guard.is_installed():
        return [
            Finding(
                WARN,
                "the guard is not installed: commits with the wrong identity are not blocked",
                GUARD_INSTALL,
            )
        ]
    hooks = paths.hooks_dir()
    scripts = (*guard.GUARDED, *guard.PASSTHROUGH, guard.LAUNCHER)
    python = guard.installed_python()
    if python is None or not all((hooks / script).is_file() for script in scripts):
        where = paths.to_git_path(hooks)
        findings.append(
            Finding(ERROR, f"some of the guard's hooks are missing from {where}", GUARD_INSTALL)
        )
    elif not Path(python).is_file():  # the hooks then skip the check, with a warning
        findings.append(
            Finding(ERROR, f"the guard's hooks run a Python that is gone: {python}", GUARD_INSTALL)
        )
    return findings or [
        Finding(
            OK, "the guard is installed: commits and pushes with the wrong identity are blocked"
        )
    ]


def _check_repo(cwd: str | os.PathLike[str], identities: list[store.Identity]) -> list[Finding]:
    try:
        root = resolve.repo_root(cwd)
    except GitShiftError as exc:  # git refuses to work here: what it says tells why
        return [Finding(ERROR, f"git refuses to work in this repository:\n{_git_said(exc)}")]
    if root is None:
        return []  # not in a repository, or no working tree: nothing commits from here
    findings = []
    match = resolve.match_for_repo(cwd, identities)
    pin = explain.dangling_pin(cwd, identities, match)
    if pin:
        findings.append(Finding(WARN, *pin))
    eff = resolve.effective(cwd, identities)
    if match is None:
        findings.append(Finding(OK, "no identity is expected in this repo"))
    else:
        findings.append(_check_email(eff, match))
        findings += _check_ssh_key(cwd, match.identity, eff, identities)
    findings += _check_hooks_path(cwd)
    return findings


def _check_email(eff: resolve.Effective, match: resolve.Match) -> Finding:
    expected = match.identity
    if resolve.same_email(eff.email, expected.email):
        return Finding(
            OK,
            f'this repo commits as {eff.email}, as "{expected.name}" expects '
            f"(matched by {match.reason})",
        )
    why = explain.mismatch(eff, expected, match.kind, from_doctor=True)
    wanted = resolve.clean_email(expected.email)  # as git records it: no <> from a hand edit
    return Finding(
        ERROR,
        f'this repo commits as {explain.email_text(eff)}, not {wanted} ("{expected.name}"): {why}',
        explain.mismatch_fix(eff, expected),
    )


def _check_ssh_key(
    cwd: str | os.PathLike[str],
    identity: store.Identity,
    eff: resolve.Effective,
    identities: list[store.Identity],
) -> list[Finding]:
    """A keyed identity whose key is not in effect, when pushes to origin use SSH."""
    if not identity.ssh_key or eff.ssh_command == store.ssh_command(identity.ssh_key):
        return []
    url = resolve.origin_url(cwd)
    if url is None or not remotes.uses_ssh(url):
        return []  # an HTTPS remote uses no SSH key
    return [Finding(WARN, explain.ssh_problem(identity, eff), _ssh_fix(cwd, identity, identities))]


def _ssh_fix(
    cwd: str | os.PathLike[str], identity: store.Identity, identities: list[store.Identity]
) -> str:
    """use --here writes the key's command to .git/config, which beats the global config and
    the folder rules, but not these three."""
    if os.environ.get("GIT_SSH_COMMAND"):
        return "unset the GIT_SSH_COMMAND environment variable"
    origin = gitconfig.get_with_origin("core.sshCommand", cwd=cwd)
    source = resolve.classify(origin, identities, cwd)[0] if origin else None
    if source == "worktree":
        return "git config --worktree --unset-all core.sshCommand"
    if source == "command line":
        return "drop the -c core.sshCommand option or GIT_CONFIG_* variable"
    return f"git shift use {identity.name} --here"


def _check_hooks_path(cwd: str | os.PathLike[str]) -> list[Finding]:
    """A core.hooksPath git uses here instead of the guard's: the repository's own config,
    or a global [includeIf] that applies here."""
    if not guard.is_installed():
        return []  # the guard check already says so
    origin = gitconfig.get_with_origin("core.hooksPath", cwd=cwd)
    if origin is None or guard.is_hooks_dir(origin.value):
        return []
    where = origin.path if origin.kind == "file" else explain.PLACES.get(origin.kind, origin.kind)
    return [
        Finding(
            WARN, f"the guard does not run in this repository: core.hooksPath is set in {where}"
        )
    ]
