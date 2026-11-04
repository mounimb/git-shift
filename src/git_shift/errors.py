"""Errors shown to the user as plain messages, without a traceback."""


class GitShiftError(Exception):
    """An expected problem, such as invalid input or a failed git command."""

    exit_code = 2
