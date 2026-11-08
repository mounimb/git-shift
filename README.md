# git-shift

[![CI](https://github.com/mounimb/git-shift/actions/workflows/ci.yml/badge.svg)](https://github.com/mounimb/git-shift/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Folder-based git identities, with a guard against committing as the wrong account.**

If you use several GitHub accounts on one machine (personal, work, open source), git's
single global identity means commits end up credited to the wrong account, and you only
notice after pushing. `git-shift` binds each folder to an identity (name, email, SSH key,
GitHub login), tells you which identity applies anywhere and why, and blocks commits and
pushes made with the wrong one.

I built it because commits on my personal repositories kept landing on my other GitHub
account.

## Who it is for

Developers with more than one git identity on the same computer, on Windows, macOS or
Linux. It works from the terminal and from any GUI client (VS Code, GitHub Desktop,
JetBrains IDEs), because it relies on git's own configuration and hooks.

## Install

Requires Python 3.10+ and git 2.13+ (2.18+ for the guard's rebase hint). The GitHub CLI
(`gh`) is optional.

```bash
pipx install git+https://github.com/mounimb/git-shift
```

This puts a `git-shift` executable on your `PATH`, so git runs it as `git shift`.
`pip install git+https://github.com/mounimb/git-shift` works too. To install a given
release, add its tag (`...git-shift@v0.1.0`), or download the wheel from the
[releases page](https://github.com/mounimb/git-shift/releases).

## Quick start

```bash
git shift add perso --email jane@example.com --name "Jane Doe" \
    --folder ~/code/perso --ssh-key ~/.ssh/id_perso --gh jane
git shift add work --email jane@work.example.com --name "Jane Doe" --folder ~/code/work
git shift guard install
```

Every repository under `~/code/perso` now commits as `jane@example.com` and pushes with
`~/.ssh/id_perso`, in every git client. `status` shows which identity applies, and why:

```text
$ cd ~/code/perso/app
$ git shift status
repo:     ~/code/perso/app
expected: "perso" <jane@example.com>  (matched by folder ~/code/perso/)
name:     Jane Doe
email:    jane@example.com  (from identity "perso")
ssh:      ssh -i ~/.ssh/id_perso -o IdentitiesOnly=yes
```

A repository cloned outside your folders is still recognised when its `origin` belongs to
one of your GitHub logins, and the guard stops the commit before it happens:

```text
$ cd ~/Desktop/side-project        # origin: https://github.com/jane/side-project.git
$ git commit -m "add x"
✗ git-shift: this repo belongs to "perso" (jane), but you are committing as jane@work.example.com
  expected: jane@example.com  (matched by GitHub owner jane)
  fix:      git shift use perso --here
  why:      git shift status shows where the wrong email comes from
  amend:    your own commit: add --reset-author; someone else's: --no-verify
  --author: drop it
  bypass:   git commit --no-verify
```

Commits that got past the commit check (made with `--no-verify`, a merge, or before the
guard was installed) are caught when you push. Only commits the remote does not have yet
are checked, and the message says how to re-author them:

```text
$ git push origin main
✗ git-shift: 1 commit to push is not authored by "perso" <jane@example.com> (matched by folder ~/code/perso/)
  2f3f7d6  jane@work.example.com
  config:   already right (jane@example.com): only these commits need re-authoring
  then:     on main: git commit --amend --reset-author --no-edit
  bypass:   pushing someone else's commits? git push --no-verify
```

With several wrong commits, the `then:` line is a `git rebase -r --exec ...` that
re-authors only the listed commits, so a teammate's merged work keeps its author.

`doctor` checks the whole setup and gives a fix for each problem:

```text
$ git shift doctor
✓ git 2.56
✓ 2 identities: perso, work
✓ ~/.gitconfig includes rules.gitconfig, and nothing after it overrides it
✓ rules.gitconfig matches your identity files
✓ gh not installed or not responding: GitHub CLI checks skipped
✓ the guard is installed: commits and pushes with the wrong identity are blocked
✗ this repo commits as jane@work.example.com, not jane@example.com ("perso"): overridden by this repo's local config (.git/config)
  fix: git shift use perso --here
```

## Commands

| Command | What it does |
|---|---|
| `git shift add <identity> --email E --name AUTHOR --folder DIR [--ssh-key K] [--gh LOGIN]` | Create an identity bound to a folder |
| `git shift list` | List identities |
| `git shift status` | Show which identity applies here, and why |
| `git shift use <identity> --here` | Pin an identity on the current repository |
| `git shift remove <identity>` | Delete an identity |
| `git shift doctor [--fix]` | Check the setup, with a fix for each problem; `--fix` rewrites `rules.gitconfig` |
| `git shift guard install` / `uninstall` | Block commits and pushes made with the wrong identity |

Use `git shift -h` or `git shift <command> -h` for help (`git shift --help` makes git look
for a man page, which does not exist yet).

Exit codes: 0 when all is well, 1 when `status` or `doctor` finds a problem (or on an
unexpected error), 2 for invalid input or a failed git command, 130 on Ctrl-C.

## How it works

- Each identity is a small git config file in `~/.config/git-shift/identities/`.
- `~/.config/git-shift/rules.gitconfig` maps folders to identities with git's own
  conditional includes (`includeIf "gitdir/i:..."`). Your global config gets a single
  `include` line that points to it. Because this is plain git configuration, it works in
  every git client, even when `git-shift` is not running.
- `git shift use <identity> --here` pins a repository: it writes the identity's
  `user.name`, `user.email` and `core.sshCommand` to the repository's own config, plus a
  `gitshift.identity` key. The pin wins over the folder rules, and the guard follows it.
- The guard sets a global `core.hooksPath`. Its `pre-commit` hook checks the author email
  git is about to record. Its `pre-push` hook checks every commit the push publishes for
  the first time; commits already on a remote-tracking branch (a teammate's work you
  merged) are never flagged. The repositories' own hooks keep running: the guard chains
  to them.
- The expected identity comes from the repository's pin, then its folder, then the GitHub
  owner of its `origin` (or of the push URL).
- If `git-shift` itself fails inside a hook, the hook prints a warning and lets git go on:
  a bug in the tool must never block every commit on the machine.
- `git-shift` changes git configuration through `git config`, and backs up your global
  config before changing it. There is one exception: if your global config already has an
  `[include]` section above your `[user]` settings, `git config --add` would put the
  git-shift include there, where your global email would override every identity. In that
  case git-shift appends a new `[include]` section at the end of the file instead. It
  writes through git's own lock file and checks that git reads the result as before plus
  that one line; otherwise it leaves the file untouched.

### Undoing things

- Bypass the guard once: `git commit --no-verify`, `git push --no-verify`, or
  `GIT_SHIFT_SKIP=1` (which skips the check but still runs the repository's own hooks).
- Remove the guard: `git shift guard uninstall`. It restores the `core.hooksPath` you had
  before.
- Unpin a repository: `git config --local --remove-section gitshift`, then remove the
  `user.name`, `user.email` and `core.sshCommand` it wrote from `.git/config`.
- `git shift remove` does not touch repositories. A pin on a removed identity is ignored,
  and `status` reports it.

## Limitations

- A repository that sets its own `core.hooksPath` (husky, for example) bypasses the global
  guard, and so does a global `core.hooksPath` set only for some folders with
  `[includeIf]`. `git shift doctor` tells you when the guard does not run in a repository.
- A global `core.hooksPath` turns off every repository's `.git/hooks`, so the guard
  installs pass-through hooks that run them. A few hook types are left out:
  `reference-transaction` and `post-index-change` run on almost every git command, and
  `update` once per pushed ref, so starting a shell for each would slow git down;
  `post-update` gets every pushed ref on its command line; `push-to-checkout` would
  replace git's built-in `updateInstead` just by existing; `proc-receive` and the git-p4
  hooks only serve review servers and the Perforce bridge.
- A merge commit made with the wrong identity is only caught when you push.
- The guard costs a few hundred milliseconds per command, mostly to start Python. Measured
  on Windows, where starting processes is slow: a commit takes about 0.4 s longer and a
  push about 0.5 s longer, up to twice that on a busy machine. Making it faster is on the
  roadmap.
- The origin-owner check only recognises `github.com` remotes. SSH host aliases such as
  `git@github-work:owner/repo` are not recognised yet (see the roadmap).
- An identity nested inside another identity's folder keeps the outer SSH key unless it
  sets its own (`git shift add` and `doctor` warn about it): git cannot unset a key from an
  include.
- Removing the last identity leaves the `include.path` line in your global config. It is
  harmless; delete it with
  `git config --global --unset include.path 'git-shift/rules\.gitconfig'` (the last
  argument is a regular expression).

## Development

```bash
git clone https://github.com/mounimb/git-shift && cd git-shift
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest
ruff check . && ruff format --check .
```

Tests run against real git in temporary home directories and never touch your own
configuration. CI runs them on Linux, macOS and Windows with Python 3.10 and 3.14. The
design and the implementation plan are in [`docs/plans/`](docs/plans/).

## Built with

Python 3.10+ (standard library only, no dependencies), git's conditional includes and
hooks, and optionally the GitHub CLI. Tested with pytest and GitHub Actions.

## Roadmap

See [ROADMAP.md](ROADMAP.md) and the [open issues](https://github.com/mounimb/git-shift/issues).

## License

[MIT](LICENSE)
