# ANYM-937: the WhatsApp session holder image (`anymsg-whatsmeow --serve`). Its build context is whatsapp/whatsmeow
# alone: no Python, no messaging_service, so ANYM-895's input digest keeps the holders (and their WhatsApp
# connections) through every release that does not change this directory. GO_IMAGE equals services/messaging/Dockerfile's
# pin (tests/test_whatsapp_holder_image.py).
ARG GO_IMAGE=golang:1.26.10-bookworm@sha256:23ce71337b726608afb1bc00986e630a05034a87a87ea1fc2cd1ae5cf33387eb
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . ./
ARG DEEDKIT_SOURCE_SHA
# The source revision is the holder's hello `holder_source_sha`; timetzdata: the image has no zoneinfo.
RUN CGO_ENABLED=0 go build -trimpath -tags timetzdata -ldflags "-X main.sourceSHA=${DEEDKIT_SOURCE_SHA:-unknown}" \
        -o /out/anymsg-whatsmeow . \
    && mkdir -p /out/licenses \
    && go list -m all > /out/licenses/modules.txt \
    && find /go/pkg/mod -type f \( -iname "license*" -o -iname "copying*" -o -iname "notice*" \) -exec cp --parents {} /out/licenses \;

FROM scratch AS whatsapp-holder
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/anymsg-whatsmeow /usr/local/bin/anymsg-whatsmeow
COPY --from=build /out/licenses /licenses
# The holder protocol range (serve.go holderSupported); ANYM-896's pair rule reads it from the image.
LABEL io.deedkit.holder.protocol.min="1" io.deedkit.holder.protocol.max="1"
# The collector's UID: the session, socket and events directories are 10001's, mode 0700.
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/anymsg-whatsmeow"]
CMD ["--serve", "/run/deedkit/holder", "--config", "/run/secrets/whatsapp_config"]
ARG DEEDKIT_SOURCE_SHA
ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA
