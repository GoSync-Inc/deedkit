ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:51b1100cc2a83d370c6a60952e3f2989c8a43159d0e38586e090f3b3326efefd
ARG POSTGRES_IMAGE=postgres:18-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c
ARG GO_IMAGE=golang:1.26.10-bookworm@sha256:23ce71337b726608afb1bc00986e630a05034a87a87ea1fc2cd1ae5cf33387eb
FROM ${GO_IMAGE} AS whatsmeow-build
WORKDIR /src
COPY whatsapp/whatsmeow/go.mod whatsapp/whatsmeow/go.sum ./
RUN go mod download
COPY whatsapp/whatsmeow ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/anymsg-whatsmeow . \
    && mkdir -p /out/licenses \
    && go list -m all > /out/licenses/modules.txt \
    && find /go/pkg/mod -type f \( -iname "license*" -o -iname "copying*" -o -iname "notice*" \) -exec cp --parents {} /out/licenses \;

FROM ${NODE_IMAGE} AS node-runtime
FROM node-runtime AS whatsapp-dependencies
WORKDIR /whatsapp
ENV PUPPETEER_SKIP_DOWNLOAD=true
COPY whatsapp/package*.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
FROM ${PYTHON_IMAGE} AS python-dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/messaging/requirements.lock ./requirements.lock
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY services/messaging/requirements-telemetry.lock ./requirements-telemetry.lock
ARG ANYMSG_INSTALL_TELEMETRY=0
RUN if [ "$ANYMSG_INSTALL_TELEMETRY" = "1" ]; then pip install --no-cache-dir --require-hashes -r requirements-telemetry.lock; fi
RUN groupadd -g 10001 probe && useradd -u 10001 -g probe -M probe \
    && mkdir -p /data/files/.uploads && chmod 0700 /data/files/.uploads && chown -R probe:probe /data
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe

FROM python-dependencies AS runtime
COPY packages/platform-contracts/src/platform_contracts ./platform_contracts
COPY services/messaging/messaging_service ./messaging_service
# Existing Telegram owner extension uses the collector's session and governor.
COPY services/calls/telegram_broker.py services/calls/telegram_session_owner.py ./
EXPOSE 8080
CMD ["python", "-m", "messaging_service", "serve"]
# Exact source commit for telemetry (TELEMETRY.md); last, so a new commit reuses every cached layer.
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA

FROM python-dependencies AS indexer
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
# Debian packages follow security updates within the digest-pinned base release; image scans verify the result.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus \
    && dpkg-query -W > /app/indexer-system-packages.txt && rm -rf /var/lib/apt/lists/*
COPY services/messaging/messaging_service ./messaging_service
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
CMD ["python", "-m", "messaging_service.file_index"]
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA

FROM indexer AS test
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
COPY packages/platform-contracts/src/platform_contracts ./platform_contracts
# Contract tests inspect the same public deployment and workflow templates
# distributed in the source checkout. No private files or Docker socket.
COPY services/messaging/Dockerfile ./services/messaging/Dockerfile
COPY deploy ./deploy
COPY .github/workflows ./.github/workflows
COPY scripts ./scripts
COPY config ./config
COPY services/messaging ./services/messaging
COPY services/messaging/anymsg ./anymsg
COPY pyproject.toml README.md LICENSE .dockerignore ./
COPY services/messaging/pyproject.toml ./services/messaging/pyproject.toml
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=whatsmeow-build /out/anymsg-whatsmeow /usr/local/bin/anymsg-whatsmeow
ENV ANYMSG_WHATSMEOW_TEST_BINARY=/usr/local/bin/anymsg-whatsmeow
COPY whatsapp ./whatsapp
COPY services/messaging/requirements-test.lock ./requirements-test.lock
RUN pip install --no-cache-dir --require-hashes -r requirements-test.lock
COPY tests ./tests
COPY media_benchmark.py ./media_benchmark.py
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", "tests"]

# Optional enrichment image. Neither native audio tools nor speech dependencies
# are installed in the gateway, collector, indexer or regular test stages.
FROM python-dependencies AS enricher
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
# Debian packages follow security updates within the digest-pinned base release; image scans verify the result.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 \
    && dpkg-query -W > /app/enricher-system-packages.txt && rm -rf /var/lib/apt/lists/*
COPY services/messaging/messaging_service ./messaging_service
# Fixed offline/telemetry booleans, with no credential value.
# hadolint ignore=DL3064
ENV HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
CMD ["python", "-m", "messaging_service.enrichment", "--config", "/run/secrets/enrichment_config"]

# CPU inference only; model weights are installed separately and mounted read-only.
FROM enricher AS enricher-local
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
COPY services/messaging/requirements-enrichment.lock ./requirements-enrichment.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-enrichment.lock \
    && pip check
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA

# Isolated smoke target. No database, credentials, model downloads or external
# API calls are needed for native WAV/OGG and mocked provider tests.
FROM enricher-local AS enricher-test
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
COPY services/messaging/requirements-test.lock ./requirements-test.lock
RUN pip install --no-cache-dir --require-hashes -r requirements-test.lock
COPY tests ./tests
ENV REQUIRE_NATIVE_AUDIO=1
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", "tests/test_audio_native.py"]

# Default for new accounts: a single native session owner, no browser or Node.
FROM python-dependencies AS whatsapp-native
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
# Debian packages follow security updates within the digest-pinned base release; image scans verify the result.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=whatsmeow-build /out/anymsg-whatsmeow /usr/local/bin/anymsg-whatsmeow
COPY --from=whatsmeow-build /out/licenses /app/whatsmeow-licenses
COPY services/messaging/messaging_service ./messaging_service
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
CMD ["python", "-m", "messaging_service.whatsapp_collector", "--config", "/run/secrets/whatsapp_config"]
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA

# Explicit rollback image for existing browser sessions; never auto-converted.
FROM python-dependencies AS whatsapp
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER root
ARG CHROMIUM_VERSION=154.0.8037.92-1~deb12u1
# Debian packages follow security updates within the digest-pinned base release; image scans verify the result.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends chromium=${CHROMIUM_VERSION} ca-certificates \
    && dpkg-query -W > /app/system-packages.txt && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=whatsapp-dependencies /whatsapp/node_modules ./whatsapp/node_modules
COPY whatsapp ./whatsapp
COPY services/messaging/messaging_service ./messaging_service
ENV HOME=/tmp PUPPETEER_SKIP_DOWNLOAD=true
# This user is defined by the pinned base or explicitly created above.
# hadolint ignore=DL3066
USER probe
CMD ["python", "-m", "messaging_service.whatsapp_collector", "--config", "/run/secrets/whatsapp_config"]

# Operator-only image; no Docker socket or SSH key. The producer receives no
# recovery key; only the isolated restore helper gets an explicit read-only key.
# The cold archiver mounts its own forced-command SSH key at runtime only.
FROM ${POSTGRES_IMAGE} AS backup
# Backup reads existing collector-owned files; deploy drops capabilities and restricts mounts (BACKUP.md).
# hadolint ignore=DL3002,DL3066
USER root
# Debian packages follow security updates within the digest-pinned base release; image scans verify the result.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    libsqlite3-0 libbz2-1.0 libffi8 liblzma5 libexpat1 libreadline8 libncursesw6 libgdbm6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 probe && useradd -u 10001 -g probe -M -d /nonexistent -s /usr/sbin/nologin probe
COPY --from=python-dependencies /usr/local /usr/local
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python -c "import hashlib,io,tarfile,urllib.request; b=urllib.request.urlopen('https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz').read(); assert hashlib.sha256(b).hexdigest()=='cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10'; t=tarfile.open(fileobj=io.BytesIO(b)); m=t.getmember('age/age'); assert m.isfile(); f=open('/usr/local/bin/age','xb'); f.write(t.extractfile(m).read()); f.close()" \
    && chmod 0755 /usr/local/bin/age && age --version && pg_dump --version
COPY services/messaging/messaging_service ./messaging_service
# The receiver lock its update follow checks a staged release against (BACKUP_R2.md, ANYM-597).
COPY services/messaging/requirements-backup-r2.lock ./requirements-backup-r2.lock
COPY licenses/age-LICENSE /usr/share/doc/age/LICENSE
ENTRYPOINT []
CMD ["python", "-m", "messaging_service.backup_worker", "--config", "/run/secrets/backup_config"]
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA
