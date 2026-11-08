"""Errors shown to the user as plain messages, without a traceback."""


class GitShiftError(Exception):
    """An expected problem, such as invalid input or a failed git command."""

    exit_code = 2


class GitCommandError(GitShiftError):
    """A git command that failed. stderr is what git said: it names the file at fault."""

    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr
