"""Library telemetry switches, dependency-free. Importing the package applies them (`__init__.py`), so every process
of this service that later imports an engine library (a worker, a command run by the installer, a smoke test) has
them set first. Offline switches are not here: `pull` needs the network (engines/offline.py)."""

import os

TELEMETRY = {
    "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "ORT_DISABLE_TELEMETRY": "1", "ANONYMIZED_TELEMETRY": "False",
}


def apply():
    os.environ.update(TELEMETRY)
