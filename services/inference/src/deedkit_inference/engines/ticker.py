"""Test-only progress oracle for the yielding tests (ANYM-691): `tick` appends one byte to `<path>.main` every 50 ms
from the calling process and one to `<path>.helper` from a helper in the same process group. A stopped group stops
growing both files; `count` rounds are the work, so a pause delays the end and changes nothing else."""

import subprocess
import sys
import time

HELPER = ("import sys, time\nfor _ in range(int(sys.argv[2])):\n    open(sys.argv[1], 'a').write('x')\n"
          "    time.sleep(0.05)\n")


def tick(path, count):
    helper = subprocess.Popen([sys.executable, "-I", "-c", HELPER, path + ".helper", str(count + 100)])
    try:
        for _ in range(count):
            with open(path + ".main", "a") as handle:
                handle.write("x")
            time.sleep(0.05)
    finally:
        helper.kill()
        helper.wait()
