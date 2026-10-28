# The autoloop runner image (ANYM-574, docs/operations/feedback-autoloop.md, Container runner): the agent CLI, git,
# gh, uv with Python 3.13, node and the Tiden CLI, every one pinned. launch.py builds it, tagged by the hash of this
# directory, and runs it on an internal network whose only way out is proxy.py.
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM node:24-bookworm-slim@sha256:8579e5fce559544b50072d05cc5d3b84b6a14d0989ae56c331d9e983a36a21f1 AS tools
ARG TARGETARCH
ARG GH_VERSION=2.103.3
ARG GH_SHA256_amd64=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
ARG GH_SHA256_arm64=ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63
ARG TIDEN_VERSION=0.42.0
ARG TIDEN_SHA256_amd64=691850035886a903f67e3650e578786db1cd7418ab7d7296e9666ef7f3724380
ARG TIDEN_SHA256_arm64=1a2717d4b3563c0b80551e5e2d742a9ef6fe50e83284cb6f1816019a4dc349d5
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/tools
RUN case "$TARGETARCH" in amd64) gh_sum=$GH_SHA256_amd64 tiden_sum=$TIDEN_SHA256_amd64 ;; \
      arm64) gh_sum=$GH_SHA256_arm64 tiden_sum=$TIDEN_SHA256_arm64 ;; *) exit 1 ;; esac \
    && curl -fsSLo gh.tar.gz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" \
    && echo "$gh_sum  gh.tar.gz" | sha256sum --check --strict \
    && curl -fsSLo tiden.tar.gz "https://github.com/qase-tms/homebrew-tap/releases/download/v${TIDEN_VERSION}/tiden_linux_${TARGETARCH}.tar.gz" \
    && echo "$tiden_sum  tiden.tar.gz" | sha256sum --check --strict \
    && mkdir -p /out && tar -xzf gh.tar.gz --strip-components=2 -C /out "gh_${GH_VERSION}_linux_${TARGETARCH}/bin/gh" \
    && tar -xzf tiden.tar.gz -C /out tiden

FROM node:24-bookworm-slim@sha256:8579e5fce559544b50072d05cc5d3b84b6a14d0989ae56c331d9e983a36a21f1
ARG CLAUDE_CODE_VERSION=2.1.310
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_PREFERENCE=only-managed UV_LINK_MODE=copy \
    DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_TELEMETRY=1 \
    GH_NO_UPDATE_NOTIFIER=1 GH_PROMPT_DISABLED=1 NPM_CONFIG_UPDATE_NOTIFIER=false
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=tools /out/gh /out/tiden /usr/local/bin/
RUN npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && uv python install 3.13 && chmod -R a+rX /opt/uv-python \
    && useradd --create-home --uid 10001 --shell /bin/bash runner
COPY --chmod=0555 proxy.py selfcheck.py entrypoint.sh /opt/runner/
USER runner
WORKDIR /home/runner
RUN printf '{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true}\n' > /home/runner/.claude.json
ENTRYPOINT ["/opt/runner/entrypoint.sh"]
