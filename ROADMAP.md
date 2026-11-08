# Roadmap

Planned work after v0.1, roughly in priority order. Each item has a GitHub issue with the
`roadmap` label; estimates are rough.

| Feature | Issue | Estimate |
|---|---|---|
| Match identities by remote URL as well as folders (`includeIf "hasconfig:remote.*.url:..."`, git 2.36+) | [#1](https://github.com/mounimb/git-shift/issues/1) | 4 h |
| `git shift fix`: re-author unpushed commits made with the wrong identity | [#2](https://github.com/mounimb/git-shift/issues/2) | 4 h |
| Faster guard hooks: fewer git processes per check, a lighter Python start | [#13](https://github.com/mounimb/git-shift/issues/13) | 3 h |
| Automatic `gh` account switching when entering an identity's folder | [#3](https://github.com/mounimb/git-shift/issues/3) | 4 h |
| `git shift ssh new <identity>`: generate an SSH key and add it to GitHub through `gh` | [#4](https://github.com/mounimb/git-shift/issues/4) | 3 h |
| Per-identity commit signing (SSH or GPG) | [#5](https://github.com/mounimb/git-shift/issues/5) | 3 h |
| `git shift import`: build identities from an existing `~/.gitconfig` and `~/.ssh/config` | [#6](https://github.com/mounimb/git-shift/issues/6) | 3 h |
| HTTPS credentials per identity (`credential.<url>.username`) | [#7](https://github.com/mounimb/git-shift/issues/7) | 3 h |
| Recognise SSH host aliases (`git@github-work:...`) in the origin-owner check, via `ssh -G` | [#8](https://github.com/mounimb/git-shift/issues/8) | 2 h |
| Shell completion for bash, zsh and PowerShell | [#9](https://github.com/mounimb/git-shift/issues/9) | 2 h |
| Scoop and Homebrew packages | [#10](https://github.com/mounimb/git-shift/issues/10) | 2 h |
| Man page, so `git shift --help` works | [#11](https://github.com/mounimb/git-shift/issues/11) | 1 h |
| Documentation site with recipes (work/personal split, open-source maintainers) | [#12](https://github.com/mounimb/git-shift/issues/12) | 2 h |

About 36 hours in total, plus the bugs and feedback that come with real use.
