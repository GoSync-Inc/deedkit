"""Stop the owned Maestro process group if its service parent exits unexpectedly."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def stop_group(group, signum):
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can return EPERM for a group containing only unreaped zombies.
        rows = subprocess.check_output(['/bin/ps', '-ax', '-o', 'pgid=,stat='], text=True)
        for row in rows.splitlines():
            fields = row.split()
            if len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith('Z'):
                raise


def main():
    parent = int(sys.argv[1])
    temporary = Path(sys.argv[2])
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process = subprocess.Popen(sys.argv[3:], start_new_session=True)
    try:
        while process.poll() is None and not stopping and os.getppid() == parent:
            time.sleep(0.1)
        return process.returncode if process.returncode is not None else 1
    finally:
        # The child owns a separate group. Stop descendants even if its launcher exited.
        stop_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        stop_group(process.pid, signal.SIGKILL)
        process.wait()
        shutil.rmtree(temporary, ignore_errors=True)



if __name__ == '__main__':
    sys.exit(main())
