"""The commit this calls service was built from and its start time (ANYM-662), read like messaging's build."""
from datetime import datetime, timezone
import os
import re

STARTED_AT = datetime.now(timezone.utc)


def build():
    """`source_sha` is the 40-hex DEEDKIT_SOURCE_SHA of the image, else None; an invalid value reads like an absent one."""
    sha = os.environ.get("DEEDKIT_SOURCE_SHA", "")
    return {"source_sha": sha if re.fullmatch("[0-9a-f]{40}", sha) else None,
            "started_at": STARTED_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
