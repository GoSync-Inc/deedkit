FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS build
RUN pip install --no-cache-dir uv==0.9.26
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
RUN useradd --uid 10001 --no-create-home recordings && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["deedkit-recordings"]
CMD ["--config", "/run/deedkit/recordings/config.json", "--host", "0.0.0.0", "--port", "13234"]
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA
