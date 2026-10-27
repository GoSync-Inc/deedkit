# AnyMsg

**Self-hosted Telegram and WhatsApp archive for AI agents. A GoSync open-source project.**

Collect messages and attachments once, then let agents search and read them through
MCP or HTTP with separate credentials and account/chat permissions. Original files
use SHA-256 deduplication; references to every source message stay separate.

[Conversation view and compatibility](CONVERSATION.md) explain message kinds,
ordering uncertainty and the last known dialogue turn.

Development version: **0.9.19 — experimental**, PostgreSQL schema 49.

The expanded live-operation catalog covers messages, registered attachments,
reactions, groups, contacts and profile fields. Access requires explicit per-key
grants and account opt-in; see [MESSENGER_OPERATIONS.md](MESSENGER_OPERATIONS.md).
Published npm/PyPI release: **0.9.9**; sending and independent WhatsApp IPC lanes
require the development source until the next package release.

## What works

- Multiple Telegram and WhatsApp accounts, each with its own session and collection state.
- PostgreSQL full-text search, including Russian word forms, people/usernames,
  chat/date/sender filters, nearby context and direct provider references.
- Searchable file text, PDF/Office/table extraction, bounded OCR and optional
  audio transcription or image/document enrichment through local or API profiles.
- Per-chat history coverage, collector errors and separate download/extraction/analysis states.
- Scoped agent keys, independent acknowledgements, immutable review records and revision evidence.
- Durable collection queues, storage admission, processing budgets and a shared heavy-job lock.
- Encrypted streaming backup with integrity checks and quarantine restore tools.
- Persistent subscriptions to explicit chats and people, with passive event inspection
  and separate original/text/transcript/description readiness events.
  Independent event ACK, bounded waits and operator-registered signed hints are available;
  [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) separates hints from verified host turns.

Text sending through existing collectors is available with separate `send` rights
and explicit account opt-in. The acting agent obtains exact owner confirmation;
there is no server approval gate yet. See [SENDING.md](SENDING.md). Reads do not mark messages
as read in Telegram or WhatsApp. Empty search results do not prove complete history.
Opt-in managed eviction of unpinned originals and best-effort re-download on an
explicit original request are implemented (MEDIA_STORAGE.md). Durable history-wave
execution, a cold archive with verified rehydration and cross-occurrence AI
artifact reuse are planned. Optional cloud processing is off until explicitly configured.

## Distribution

- **Python `anymsg`**: server, operator CLI and HTTP client. Internal `messaging_service`
  imports, database names and migration checksums remain compatible.
- **npm `anymsg`**: dependency-free JavaScript HTTP client and CLI for an existing server.
  It does not install PostgreSQL, Chromium or a second messenger session.
- **Source / Docker**: [server setup](SETUP.md) and explicit collector overlays in `deploy/`.

For a source installation with Python 3.13:

```sh
git clone https://github.com/GoSync-Inc/deedkit.git
cd deedkit
uv sync --group dev
uv run anymsg --help
```

Registry installation (Python 3.13):

```sh
pip install anymsg==0.9.9
npm install anymsg@0.9.9
```

Python installs the server/operator CLI and HTTP client; npm installs only the
JavaScript HTTP client/CLI. Deployment prerequisites are in [SETUP.md](SETUP.md).
Maintainer publication instructions are in [RELEASING.md](RELEASING.md).

JavaScript client:

```js
import { AnyMsgClient } from 'anymsg';
const client = new AnyMsgClient({
  baseUrl: 'https://messages.example.com',
  token: process.env.ANYMSG_TOKEN,
});
const result = await client.search({ query: 'contract', limit: 10 });
```

Python client:

```python
import os
from anymsg import Client
with Client('https://messages.example.com', os.environ['ANYMSG_TOKEN']) as client:
    result = client.search(query='contract', limit=10)
```

The source SDK also stages local files separately from sending:

```python
with Client('https://messages.example.com', os.environ['ANYMSG_TOKEN']) as client:
    staged = client.upload_file('/local/photo.jpg', account='whatsapp-primary',
        chat_id='chat_resolved_with_this_account', mime='image/jpeg',
        request_id='photo-upload-2026-001')
# Nothing was sent. The ready receipt contains upload_id and sha256.
```

`upload_file` accepts regular non-symlink files of 1 byte–32 MiB, hashes in a
stream, uploads 256 KiB chunks and rejects changes to the original. It keeps one
descriptor open and sends only the basename, metadata and bytes, never local paths
or source URLs. `filename` is optional; MIME defaults to
`application/octet-stream`. Low-level methods are `prepare_file_upload`,
`upload_file_chunk` and passive `get_file_upload`. All require explicit scoped
upload/send/read permission and account `send_file` policy; the gateway retains
its existing download ceiling for that action. Other requests keep the 16 KiB
SDK limit; only a chunk may use a 360448-byte JSON body.

On timeout the SDK stops. Inspect the upload receipt, or explicitly invoke the
helper again with the same request ID, bytes and metadata to reconcile/resume.
It never invents another ID, retries an unknown mutation or sends automatically.
Chunks from one helper are paced below eight per second; concurrent helpers
sharing a key can still hit the server's limit. A source change can leave a staged
upload that expires normally, but never triggers a send. Sending uses a separate
reviewed `send_file` preview/action with exactly one `upload_id` or `attachment_id`,
the matching SHA-256 and `mode='photo'` or `mode='document'`, after owner approval.
The helper is synchronous; cleanup runs on completion, error or interruption.

Point a Streamable HTTP MCP client at `https://messages.example.com/mcp` and set
`Authorization: Bearer <agent key>`. Use each agent's own scoped key. The server
uses JSON POSTs; it does not provide a GET notification stream.

## Operations and limits

The tested server stack is Linux amd64, Python 3.13 and PostgreSQL 18.
The default WhatsApp collector uses a Go whatsmeow adapter without Chromium or
Node. The explicit browser fallback requires Node 24 and Chromium. Native host
installations also need PostgreSQL backup tools; the Python wheel does not install
these system dependencies or the Go binary. Windows server execution is unsupported;
Docker ARM support is not claimed.

Use separate deployments/databases for different owners. Multiple accounts in one
installation do not create a tenant security boundary. Never run two clients against
one provider session. History available to a linked WhatsApp device can be partial.
WhatsApp adapters use unofficial `whatsmeow` (default) or `whatsapp-web.js`
(explicit fallback). They are separate from the WhatsApp Business API. Check
`list_messenger_operations` for the capabilities of each configured account;
unsupported provider actions are hidden. Follow provider terms and use appropriately
authorized account/chat scopes.

See [ACCESS.md](ACCESS.md), [SEARCH.md](SEARCH.md), [FILE_INDEX.md](FILE_INDEX.md),
[COLLECTORS.md](COLLECTORS.md), [MEDIA_STORAGE.md](MEDIA_STORAGE.md),
[ENRICHMENT.md](ENRICHMENT.md) and [BACKUP_STREAMING.md](BACKUP_STREAMING.md).
Run synthetic tests only: [CONTRIBUTING.md](CONTRIBUTING.md).
Current work, priorities and delivery status: [DeedKit backlog](https://github.com/GoSync-Inc/deedkit/blob/main/BACKLOG.md).
Component responsibilities and verification boundaries: [ARCHITECTURE.md](ARCHITECTURE.md).
Historical agent review context: [REVIEW_FOLLOWUP.md](REVIEW_FOLLOWUP.md).
Optional runtime diagnostics: [TELEMETRY.md](TELEMETRY.md).

## License and security

MIT for original GoSync code. Dependencies retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `deploy/MOBY-LICENSE`.
Report vulnerabilities privately through [GitHub security advisories](https://github.com/GoSync-Inc/deedkit/security/advisories/new),
not by posting session data or tokens in an issue. See [SECURITY.md](SECURITY.md).
