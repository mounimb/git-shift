import os
import subprocess
import sys

import pytest
from helpers import git

from git_shift import __version__, paths, store
from git_shift.cli import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"git-shift {__version__}"


def test_module_entry_point():
    proc = subprocess.run(
        [sys.executable, "-m", "git_shift", "--version"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"git-shift {__version__}"


def test_add_then_list(home, capsys):
    code = main(
        [
            "add", "perso",
            "--email", "me@example.com",
            "--name", "Me",
            "--folder", str(home / "code"),
            "--gh", "mounimb",
        ]
    )  # fmt: skip
    assert code == 0
    assert 'identity "perso" created' in capsys.readouterr().out
    assert main(["list"]) == 0
    listing = capsys.readouterr().out
    assert "perso" in listing and "me@example.com" in listing and "gh: mounimb" in listing


def test_list_aligns_its_columns(home, capsys):
    for name, email, folder, extra in [
        ("perso", "jane@example.com", "code/perso", ["--gh", "jane"]),
        ("work", "jane@work.example.com", "code/work", []),
        ("lab", "lab@example.com", "lab", ["--gh", "jane-lab"]),
    ]:
        args = ["add", name, "--email", email, "--name", "Jane", "--folder", str(home / folder)]
        assert main([*args, *extra]) == 0
    capsys.readouterr()
    assert main(["list"]) == 0
    perso, work = (paths.to_folder_rule(home / "code" / name) for name in ("perso", "work"))
    lab = paths.to_folder_rule(home / "lab").ljust(len(perso))
    assert capsys.readouterr().out.splitlines() == [
        f"lab    lab@example.com        {lab}  gh: jane-lab",
        f"perso  jane@example.com       {perso}  gh: jane",
        f"work   jane@work.example.com  {work}",  # no trailing spaces
    ]


def test_list_shows_a_hand_edited_email_cleaned(home, capsys):
    args = ["add", "perso", "--email", "jane@example.com", "--name", "Jane"]
    assert main([*args, "--folder", str(home / "code")]) == 0
    path = store.identity_path("perso")
    git(home, "config", "--file", str(path), "user.email", "<jane@example.com>")  # hand-edited
    capsys.readouterr()
    assert main(["list"]) == 0
    folder = paths.to_folder_rule(home / "code")
    assert capsys.readouterr().out == f"perso  jane@example.com  {folder}\n"


def test_list_empty(home, capsys):
    assert main(["list"]) == 0
    assert "no identities yet" in capsys.readouterr().out


def test_add_invalid_email_exits_2(home, capsys):
    code = main(["add", "perso", "--email", "bad", "--name", "Me", "--folder", str(home)])
    assert code == 2
    assert "invalid email" in capsys.readouterr().err


def test_remove(home, capsys):
    main(["add", "perso", "--email", "me@example.com", "--name", "Me", "--folder", str(home)])
    assert main(["remove", "perso"]) == 0
    assert store.list_identities() == []


def test_missing_command_is_usage_error(home):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_list_flags_an_identity_without_a_usable_folder(home, capsys):
    paths.identities_dir().mkdir(parents=True)
    (paths.identities_dir() / "broken.gitconfig").write_text(
        "[user]\n\temail = broken@example.com\n", encoding="utf-8"
    )
    assert main(["list"]) == 0
    assert "(no usable folder)" in capsys.readouterr().out


def test_piped_output_is_utf8(home):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}  # what Windows uses for pipes and mintty
    args = ["add", "perso", "--email", "me@example.com", "--name", "Me"]
    args += ["--folder", str(home / "Łukasz")]
    proc = subprocess.run([sys.executable, "-m", "git_shift", *args], capture_output=True, env=env)
    out = proc.stdout.decode("utf-8")
    assert out.startswith("✓ ") and "Łukasz" in out


def test_full_flow_in_a_folder_with_a_space_and_an_accent(home, make_repo):
    # Real processes and repos, in the home fixture's environment. No --gh: the fixture hides
    # gh only in this process, so a child could start the real one. The path text is never
    # compared, as filesystems may normalise it: names and emails say what matched.
    folder = home / "Zoë code"
    repo = make_repo(folder / "app")

    def shift(*args, code=0):
        command = [sys.executable, "-m", "git_shift", *args]
        proc = subprocess.run(command, cwd=repo, capture_output=True)
        assert proc.returncode == code, proc.stderr.decode("utf-8")
        return proc.stdout.decode("utf-8")

    out = shift("add", "zoe", "--email", "zoe@example.com", "--name", "Zoë Doe", "--folder", folder)
    assert out.startswith('✓ identity "zoe" created\n')
    assert " now commit as zoe@example.com\n" in out
    shift("add", "pro", "--email", "pro@example.com", "--name", "Pro", "--folder", home / "work")

    out = shift("status")
    assert out.startswith("repo:     ")
    assert 'expected: "zoe" <zoe@example.com>  (matched by folder ' in out
    assert "name:     Zoë Doe\n" in out
    assert 'email:    zoe@example.com  (from identity "zoe")\n' in out
    git(repo, "commit", "--allow-empty", "-q", "-m", "as zoe")
    assert git(repo, "log", "-1", "--format=%an <%ae>") == "Zoë Doe <zoe@example.com>"

    out = shift("use", "pro", "--here")
    assert out == '✓ this repo now commits as pro@example.com ("pro")\n'

    out = shift("status")
    assert 'expected: "pro" <pro@example.com>  (matched by pin in .git/config)\n' in out
    assert "name:     Pro\n" in out
    assert "email:    pro@example.com  (from this repo's .git/config)\n" in out
    git(repo, "commit", "--allow-empty", "-q", "-m", "as pro")
    assert git(repo, "log", "-1", "--format=%an <%ae>") == "Pro <pro@example.com>"

    assert shift("remove", "pro") == '✓ identity "pro" removed\n'

    out = shift("status", code=1)  # the email of pro stays in .git/config
    assert 'expected: "zoe" <zoe@example.com>  (matched by folder ' in out
    assert '! this repo is pinned to "pro", which does not exist\n' in out
    assert "email:    pro@example.com  (from this repo's .git/config)\n" in out
    assert "✗ overridden by this repo's local config (.git/config)\n" in out
    assert "  fix: git shift use zoe --here\n" in out


def test_add_passes_every_option_and_prints_warnings(home, capsys):
    code = main(
        [
            "add", "perso",
            "--email", "me@example.com",
            "--name", "Jane Doe",
            "--folder", str(home / "code"),
            "--ssh-key", str(home / ".ssh" / "id_perso"),
            "--gh", "mounimb",
        ]
    )  # fmt: skip
    assert code == 0
    identity = store.load("perso")
    assert identity.user_name == "Jane Doe" and identity.gh == "mounimb"
    assert identity.ssh_key == paths.to_git_path(home / ".ssh" / "id_perso")
    out = capsys.readouterr().out
    assert "does not exist yet" in out and "not found" in out


def test_remove_unknown_identity_exits_2(home, capsys):
    assert main(["remove", "nosuch"]) == 2
    assert 'no identity named "nosuch"' in capsys.readouterr().err


def test_unexpected_error_exits_1_and_debug_raises_it(home, monkeypatch, capsys):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "list_identities", boom)
    assert main(["list"]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError: boom" in err and "git shift --debug" in err
    with pytest.raises(RuntimeError):
        main(["--debug", "list"])


def test_ctrl_c_exits_130(home, monkeypatch):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "list_identities", interrupted)
    assert main(["list"]) == 130


def test_missing_identity_name_is_called_identity(home, capsys):
    with pytest.raises(SystemExit):
        main(["add", "--email", "a@example.com", "--name", "perso", "--folder", str(home)])
    assert "IDENTITY" in capsys.readouterr().err


def test_help_says_the_guard_blocks_pushes_too(capsys):
    with pytest.raises(SystemExit):
        main(["-h"])
    assert "block commits and pushes made with the wrong identity" in capsys.readouterr().out
