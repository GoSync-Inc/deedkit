# The live test image: both engine names as fake engines, a non-root user, nothing else. Never a real engine.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
COPY live_engine.py /usr/local/lib/live_engine.py
RUN printf '#!/bin/sh\nexec python3 /usr/local/lib/live_engine.py claude "$@"\n' > /usr/local/bin/claude \
 && printf '#!/bin/sh\nexec python3 /usr/local/lib/live_engine.py codex "$@"\n' > /usr/local/bin/codex \
 && chmod 0755 /usr/local/bin/claude /usr/local/bin/codex \
 && mkdir -p /codex-home /workspace /out && chown 1000:1000 /codex-home /workspace /out
USER 1000:1000
