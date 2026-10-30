FROM golang:1.26.10-bookworm@sha256:23ce71337b726608afb1bc00986e630a05034a87a87ea1fc2cd1ae5cf33387eb AS build
WORKDIR /src
COPY services/release-router/go.mod ./
COPY services/release-router/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags="-s -w" -o /deedkit-release-router .

FROM scratch
ARG VERSION_REF
LABEL org.opencontainers.image.revision=$VERSION_REF \
      org.opencontainers.image.source="https://github.com/GoSync-Inc/deedkit"
COPY --from=build /deedkit-release-router /deedkit-release-router
# Required for the private host socket; read-only filesystem and dropped capabilities (README.md).
# hadolint ignore=DL3002
USER 0:0
ENTRYPOINT ["/deedkit-release-router"]
CMD ["--config", "/run/deedkit-router/router.json"]
