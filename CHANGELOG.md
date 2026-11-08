# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `add`, `list`, `remove`: identities (name, email, optional SSH key and GitHub login)
  bound to folders through git's conditional includes, so they apply in every git client.
- `status`: which identity applies in the current directory, where each value comes from,
  and how to fix a mismatch.
- `use --here`: pin an identity on one repository.
- Repositories outside every folder are matched by the GitHub owner of their `origin`.
- `doctor`: checks git, the identity files, the global include, `rules.gitconfig`, `gh`
  logins, the guard and the current repository, with a fix for each problem.
  `doctor --fix` rewrites `rules.gitconfig` from the identity files.
- `guard install` / `uninstall`: global `pre-commit` and `pre-push` hooks that block
  commits and pushes made with the wrong identity. They chain to the repositories' own
  hooks and to a previous global `core.hooksPath`, fail open when git-shift itself fails,
  and can be bypassed with `--no-verify` or `GIT_SHIFT_SKIP=1`.
