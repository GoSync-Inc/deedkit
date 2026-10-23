# Optional Tiden runtime diagnostics

CI results and runtime error tracking are independent. Runtime events do not add
test coverage or prove that a feature works. Error tracking is off by default.

DeedKit is the platform name. A messaging event's release is `deedkit@<sha>`,
where `<sha>` is the full 40-character lowercase source commit in
`DEEDKIT_SOURCE_SHA`. Set it only to the commit the running code was built from.
Without a valid value, for example in a package installation, a local checkout or
an image built without `DEEDKIT_SOURCE_SHA`, the release keeps the historical
`anymsg@` plus `messaging_service.__version__`; that version is shared by several
commits and does not identify a deployment. An invalid value is never sent.
Historical `anymsg@` releases and their issue links are preserved, not relabelled.
An event alone only names its release: the Tiden release record's environment, CI
build URL and gate are a separate step. See the
[Tiden readiness workflow](docs/operations/tiden-product-workflow.md).

Install `anymsg[telemetry]` or build the intended Docker target with
`--build-arg ANYMSG_INSTALL_TELEMETRY=1`. This uses the official, hash-pinned
`tiden-telemetry` Python SDK. No browser JavaScript SDK is required for AnyMsg.

Store the product ingest DSN in an owner-protected file readable by the runtime
UID. Set `ANYMSG_TIDEN_ENABLED=1`, `ANYMSG_TIDEN_DSN_FILE` to that file and
`ANYMSG_TIDEN_ENVIRONMENT` to `production` (the default) or, for a local synthetic
check, `local-synthetic`. Any other value keeps monitoring off with a content-free
warning, because Tiden silently creates a new environment for an unknown name. The
historical `synthetic` environment receives no new events (TIDEN-CI.md). Do not use
an administrative API token as the ingest DSN or commit either credential.
The optional `deploy/compose.telemetry.yaml` configures the gateway. Keep all
existing overlays and rebuild/recreate only the intended service. Workers on an
internal-only network also need an explicitly configured outbound network.

Gateway/MCP unexpected errors, fatal Telegram/WhatsApp collector errors,
unexpected indexer/enricher errors and subscription matcher errors have capture
points. Expected request validation/authorization failures and ordinary provider
retries are not automatically reported. The AI inference child is not connected.
Node/Chromium crashes are reported through the existing Python collector owner;
there is no second WhatsApp client and no browser session instrumentation.

When a Telegram or WhatsApp collector stops, its event's error code is the stop
code, so Tiden groups issues per cause, for example
`RuntimeError: capture_overload`. The exception text is reported only when it is
exactly one of the collector stop codes below; any other stop reports
`collector_failed`. A Telegram `durable_capture_failed` stop with one of the fixed
capture details of `live_capture.last_capture_stop` reports
`durable_capture_failed.<detail>` (the Telegram-only codes below); without a
detail, or with any other value, it reports `durable_capture_failed`. These
qualified codes are built only from that detail: an exception whose text is a
qualified code reports `collector_failed`, and WhatsApp never reports one. A
Telegram `capture_overload` stop names the bound that tripped (`waiters_limit` or
`slot_wait_timeout`) in `last_capture_stop` and the supervisor line only; its
event still reports `capture_overload`. The
supervisor stop line keeps its own smaller vocabulary; exception text, cause
chains and an exception's detail value never enter the event. Other components
keep their generic codes.

Tiden groups events by exception type and stack frames, and every Telegram stop is
raised at the same frames. A Telegram or WhatsApp collector event therefore also
sends the Sentry-protocol `fingerprint`, `["collector-stop", <component>, <code>]`,
built only from the validated built-in component and the code above. Each code forms
its own Tiden issue; the same code stays one issue across other exception types or
frames. `before_send` rebuilds the fingerprint from those two values and drops any
incoming `fingerprint` or title; the event sets no title. Events of other components
carry no fingerprint.

Collector stop codes: `capture_overload`, `collection_disabled`,
`collector_already_owned`, `collector_disabled`, `collector_lease_lost`,
`disk_reserve`, `durable_capture_failed`, `live_account_required`,
`private_session_required`, `provider_auth_failed`, `provider_connect_timeout`,
`provider_disconnected`, `provider_initialization_failed`, `provider_not_ready`,
`provider_page_stalled`, `provider_protocol_error`, `session_already_owned`,
`session_handover_not_confirmed`, `spool_corrupt`, `spool_full`,
`telegram_identity_mismatch`, `telegram_pairing_required`,
`whatsapp_identity_mismatch`, `whatsapp_logged_out`, `whatsapp_pairing_required`.

Telegram only: `durable_capture_failed.capture_error`,
`durable_capture_failed.disk_reserve`, `durable_capture_failed.spool_full`,
`durable_capture_failed.spool_io_error`.

The payload contains the release, environment, fixed component/error code,
allowlisted exception type, public Python filenames/line numbers and, for Telegram
and WhatsApp collector events, the `fingerprint` described above. Filenames are
relative to the repository root (`services/messaging/messaging_service/<file>.py`),
like Tiden component paths. Older events used `messaging_service/<file>.py`; component
runtime paths match both path forms (TIDEN-CI.md), and a recurring error may appear once
under each.
It excludes exception text, cause chains, locals, source lines, absolute paths,
HTTP bodies/headers/URLs, account/chat/person identifiers, search terms, messages,
files and model inputs/outputs. SDK request/user/breadcrumb extras are discarded.
Full source, logs and detailed data remain on the operator's host.

Delivery uses one background thread with a 32-event memory queue, at most 20
accepted events per minute per process and duplicate suppression for 60 seconds.
HTTP has a one-second timeout; the official SDK honors 429 backoff. Monitoring
does not wait in the request path, retry archive operations or write to the DB.
Events can be dropped during outages, overload or shutdown. Accepted locally is
not a delivery receipt: verify the event in Tiden Issues. Shutdown joins for at
most 1.5 seconds. If an ingest DSN is unreadable/invalid or the optional SDK is
missing, monitoring stays off and a content-free local warning is emitted.

Test with a synthetic exception under environment `local-synthetic`; confirm receipt
and resolve that diagnostic issue. Never provoke a real outage or use a personal
message/file as a test. Disable by removing `ANYMSG_TIDEN_ENABLED` and recreating
only the monitored service. Core archive functions require no Tiden access.
