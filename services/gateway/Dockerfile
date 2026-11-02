FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS build
RUN pip install --no-cache-dir uv==0.9.26
WORKDIR /app/services/gateway
COPY services/gateway/pyproject.toml services/gateway/uv.lock ./
COPY packages/platform-contracts /app/packages/platform-contracts
COPY services/gateway/src ./src
RUN uv sync --locked --no-dev --no-editable
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
RUN useradd --uid 10001 --no-create-home gateway
COPY --from=build /app/services/gateway/.venv /app/services/gateway/.venv
ENV PATH="/app/services/gateway/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["deedkit-gateway"]
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA
