"""The directory under which every request lives: created 0700, swept at start, byte-budgeted."""

import logging
import os
import shutil
import stat
import uuid
from pathlib import Path

from .errors import ConfigError

MARKER = ".deedkit-inference-work"
LOG = logging.getLogger("deedkit_inference.workdir")


class WorkDir:
    def __init__(self, root, max_bytes):
        self.root, self.max_bytes = Path(root), max_bytes
        self.reserved = 0

    def prepare(self):
        """Create the directory, or sweep it. A non-empty directory without our marker is refused, so a
        wrong path in the config can never make the sweep delete someone's files."""
        try:
            if not self.root.exists():
                os.makedirs(self.root, mode=0o700)
            info = os.lstat(self.root)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ConfigError("work_dir_not_an_own_directory")
            marker = self.root / MARKER
            if not marker.exists():
                if any(self.root.iterdir()):
                    raise ConfigError("work_dir_not_empty_and_not_ours")
                marker.write_text("deedkit-inference work directory\n")
            os.chmod(self.root, 0o700)
            self.sweep()
        except OSError:
            raise ConfigError("work_dir_unusable") from None

    def sweep(self):
        removed = 0
        for entry in os.scandir(self.root):
            if entry.name == MARKER:
                continue
            path = Path(entry.path)
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path, ignore_errors=True)
            removed += 1
        if removed:
            LOG.info("startup sweep removed=%d code=work_dir_swept", removed)

    def new_request_dir(self):
        path = self.root / uuid.uuid4().hex
        os.mkdir(path, 0o700)
        return path

    def remove(self, path):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
            LOG.warning("request directory not fully removed code=cleanup_incomplete")

    def reserve(self, nbytes):
        if self.reserved + nbytes > self.max_bytes:
            return False
        self.reserved += nbytes
        return True

    def release(self, nbytes):
        self.reserved = max(0, self.reserved - nbytes)
