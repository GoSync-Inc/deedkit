"""Shared by the real runtimes."""

from pathlib import Path


def require_dir(path):
    """A loader given a path that does not exist treats it as a Hub repository id and downloads. Never allow that."""
    if not Path(path).is_dir():
        raise FileNotFoundError("model directory missing")
    return str(path)
