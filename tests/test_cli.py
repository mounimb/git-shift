import subprocess
import sys

import pytest

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
