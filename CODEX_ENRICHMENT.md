# Optional Codex subscription connector

Implemented locally; not enabled by ordinary startup or deployed to production.
The connector is `provider: codex_cli` in the existing enrichment worker.
See [disabled example](enrichment.codex.example.json).

Codex runs on the worker host, but inference uses OpenAI over the network.
ChatGPT sign-in uses subscription access; an API key uses separately billed API
access. This connector requires ChatGPT sign-in and does not fall back to API
credentials. Subscription usage is shared with the owner's other Codex work,
not an unlimited batch-processing entitlement. The upstream docs describe this
as an advanced workflow for private trusted runners. Keep credentials and private
runner settings outside the public open-source repository.

## Results and supported inputs

- Images: PNG, JPEG, WebP. Short title/description/category and legible visible text.
- Documents: already extracted source text. Short title/summary/category and
  detailed **model analysis**, retained separately from the deterministic original
  text and its page/sheet/cell coordinates. No original PDF or office package is
  handed to the coding agent to execute or open with tools.
- Audio: continue using the selected ASR provider, e.g. local Whisper medium.
  This connector does not transcribe audio or replace existing ASR derivatives.
- Videos and files without usable extracted text are not automatically understood.
  They need a separate bounded extraction/frames/transcript stage.

Results remain model observations. `metadata.text_kind` is `model_analysis` for
documents and `visible_text` for images. A retained analysis is not a full exact
transcription. Long source text is bounded by `max_text_chars`; the worker keeps
its input truncation flag. The first implementation does not claim whole-document
analysis for files exceeding this limit. Future chunk/reduce jobs must cite actual
extraction coordinates and preserve partial coverage.

## Runtime boundary

Use a **dedicated private container/OS user**. The `isolated_runtime: true` field
is an operator assertion, not a mechanism that creates this boundary. The CLI is
a coding agent; a read-only sandbox alone is not a secret-read boundary.

Mount no messenger sessions, general Codex home, Docker socket, operator SSH keys,
API keys, or unrelated workspace. Give the queue worker only its dedicated DB
role, read-only originals, and the dedicated CLI authentication directory. The
native child receives no DB credentials or ambient API keys. For a stronger
boundary, stage one verified input into a separate model runner without DB/blob
mounts and ingest its JSON through the queue controller.

Install and pin a compatible official CLI in the image; never install at job
runtime. Create a dedicated `CODEX_HOME` directory with mode `0700` and an
`auth.json` with mode `0600`, owned by that runtime. Complete the official owner
login on that private runner. Do not copy the developer's general Codex home to
the server. Let the runner persist refreshes in its dedicated private directory;
never commit auth, paste codes/tokens into chat, or place it in a release artifact.
The adapter itself does not log in or transfer authentication.

Each invocation explicitly uses:

- `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, a fresh private job cwd;
- `--sandbox read-only`, approval policy `never`, forced ChatGPT auth;
- disabled shell/unified exec, apps, plugins, hooks, agents and browser;
- no MCP servers or hosted web search, no project instructions;
- source text on stdin or one controlled image; no shell interpolation;
- structured final JSON with four bounded fields; no arbitrary result keys;
- rejection of incomplete/failed runs and any observed tool-use item.

The containing enrichment worker already limits native child CPU/address space,
output file size and wall time, kills its process group on expiry and fences writes
by lease, source digest, revision, active attachment and message deletion. Do not
invoke this low-level connector on an untrusted corpus without that controller
and the separate OS/container boundary. The CLI's own model requests require
egress; its command network is disabled. Restrict runner egress separately.

`max_output_tokens` is a common API connector field, **not a verified hard token
cap for Codex CLI**. Codex outputs here are bounded by a JSON schema and response
byte/character checks, plus runtime and attempted-job limits. Do not present those
as a monetary or subscription-usage guarantee.

## Before enabling beside local ASR

State of each prerequisite, checked against the code. Paths are in
`services/messaging/messaging_service/`.

**Done**

- Separate attempt counts per profile. `processing_budget` is keyed by day,
  account, kind and profile hash (`migrations/020_processing_admission.sql:3-9`)
  and is incremented per claimed attempt (`enrichment.py:262-265`,
  `file_understanding.py:422-424`).
- A persistent profile pause for authentication failure, a required CLI upgrade
  and an unavailable CLI. `resource_admission.pause_profile` stores it per
  account, kind and profile hash in `processing_control`, without an end time for
  `codex_chatgpt_login_required`, `codex_private_auth_required`,
  `codex_login_check_failed`, `codex_upgrade_required` and
  `codex_cli_unavailable` (`resource_admission.py:120-132`; called at
  `enrichment.py:327`). Claims skip a paused scope (`enrichment.py:241`). A new
  profile hash starts unpaused. `file_understanding.py:400,469` repeats this logic
  for the separate understanding worker, which does not run `codex_cli`.

**Partly done**

- Usage exhaustion. The adapter maps a usage or rate limit to `model_rate_limited`
  (`codex_enrichment.py:122`), which pauses that scope for a fixed 30 minutes
  (`resource_admission.py:125-132`). Nothing reads the reset time the CLI may
  report, so a longer subscription window is retried every 30 minutes.

**Missing**

- Global and per-account daily counters are shared. The global ceiling
  `max_jobs_per_day` reads `enrichment_budget`, which every claim increments
  (`enrichment.py:214-217,262`, `file_understanding.py:371-373,422`). The
  per-account cap sums `processing_budget` over all kinds and profiles
  (`enrichment.py:219-229`, `file_understanding.py:389-390`). ASR attempts
  therefore count against a small Codex limit.
- The per-kind cap `profile_max_jobs_per_day` sums every profile and provider of
  that kind (`enrichment.py:221-224`, `file_understanding.py:385-386`). This is
  intended and pinned by
  `tests/test_processing_admission.py::test_cooldown_persists_and_new_profile_does_not_reset_day_limit`,
  so a cap for one profile alone would be new behaviour.
- Per-file retries of errors that do not pause. `pause_profile` ignores every
  code outside its list (`resource_admission.py:125-126`), e.g. `codex_run_failed`,
  `codex_run_incomplete` and `codex_tool_use_rejected`. Each file is attempted up
  to three times, 10 minutes apart (`enrichment.py:242-244,303`), so a systematic
  failure still walks the whole corpus.

Keep the example disabled (`enabled: false` in `enrichment.codex.example.json`)
until the missing and partly done controls, isolated deployment and owner login are complete,
or the owner accepts the open items. Whether to build them is decided under
ANYM-459. Start with a small representative set and measure quality, attempts,
latency and shared usage before enabling all accounts/files. Nothing
automatically changes the live audio profile.

## Validation

Unit tests cover opt-in, auth permissions/symlinks, API fallback rejection,
ambient credential removal, controlled invocation, supported image types,
structured result validation, timeout redaction, incomplete/tool-use rejection
and safe error classification. A real synthetic-text smoke must also pass with
the pinned CLI and chosen model before deployment. A text smoke is not a vision
quality benchmark or proof of production container isolation.

## Official sources

- [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Authentication](https://learn.chatgpt.com/docs/auth)
- [Shared subscription usage](https://learn.chatgpt.com/docs/pricing)
- [CLI installation](https://learn.chatgpt.com/docs/cli)
