# Inference contract fixtures (ANYM-682 / ANYM-685)

The requests the enrichment provider `deedkit_inference` (ANYM-685) sends to the inference service, and the
service's golden answers. Both sides test against these files, so neither can drift alone:

- ANYM-682 (`services/inference/tests/test_routes_and_strict_schema.py`) validates every request against the
  service schema, posts it to the service with the fake engine and compares the answer with the golden file.
- ANYM-685 builds the same requests with the real provider code and compares them with these files.

Files:

| file | content |
|---|---|
| `chat_image.request.json`, `chat_document.request.json` | `POST /v1/chat/completions`: `headers` (besides `Authorization: Bearer <key>`, which the client adds) and the exact `json` body. The prompt is `enrichment_providers.PROMPT`; `max_tokens` is the profile's `max_output_tokens` (default 2000) |
| `audio_transcription.request.json`, `audio_transcription_verbose.request.json` | `POST /v1/audio/transcriptions`: `multipart.fields` and the `file` part (`filename`, `content_type`, `base64`); the multipart boundary is the client's |
| `*.response.json` | the golden answer: `status` and `body`. In the chat bodies `id` is `chatcmpl-<request id>` and `created` is `0`: a comparison replaces both |
| `errors.golden.json` | the error envelope of the service's own answers, by name |

`X-Request-Deadline-Seconds` is the profile's `timeout_seconds`. Model values are aliases (`vision-default`,
`document-default`, `audio-default`), never repository ids. Everything here is synthetic.
