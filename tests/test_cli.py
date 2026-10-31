import subprocess
import sys

import pytest

from git_shift import __version__
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
