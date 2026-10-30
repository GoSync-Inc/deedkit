FROM node:24-bookworm-slim@sha256:51b1100cc2a83d370c6a60952e3f2989c8a43159d0e38586e090f3b3326efefd AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
RUN pip install --no-cache-dir uv==0.9.26
# Debian packages follow security updates within the digest-pinned base release; image scans verify the result.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libsndfile1 libopus0 ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache && groupadd -g 10001 voiceworker && useradd -u 10001 -g voiceworker -M voiceworker
COPY *.py ./
COPY --from=frontend /frontend/dist ./frontend/dist
ENV PATH=/app/.venv/bin:$PATH VOICE_BIND=0.0.0.0 VOICE_PORT=13842 VOICE_DATA_DIR=/data VOICE_EXTERNAL_MCP=0 VOICE_PERSIST_CREDENTIALS=1
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 CMD ["python", "healthcheck.py"]
CMD ["python", "app.py"]
