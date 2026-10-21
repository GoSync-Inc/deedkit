# DeedKit

**A modular platform where agents delegate messaging, calendar and voice tasks.**

This is the existing AnyMsg repository. The messaging packages keep the names
`anymsg` and `messaging_service`; their accounts, database and APIs remain compatible.

| Module | Source | Runtime |
| --- | --- | --- |
| Common MCP gateway | `services/gateway` | Selected tools, per-agent credentials and exact action ceilings |
| Messaging (AnyMsg) | `services/messaging` | Telegram/WhatsApp archive and domain operations |
| Family calendar | `services/calendar` | Google API/CalDAV, family roles, explicit coverage and plans |
| Voice calls | `services/calls` | Scoped briefs, calls, results and owner decisions |

Each module has its own environment and state. Install only the modules you need.
The gateway communicates over HTTP and does not install provider libraries.

```sh
python scripts/deedkit.py resolve --profile config/profiles/calendar-reader.toml --json
uv sync --project services/gateway --locked
uv sync --project services/calendar --locked
```

See [runtime and access](docs/platform-runtime.md), [architecture](ARCHITECTURE.md),
[verification boundaries](docs/quality/platform-acceptance.md) and
[messaging layout](docs/migrations/messaging-layout.md).
Deployment profiles require explicit private configuration before `up`; resolving a
profile does not connect accounts or authorize external actions.

## Messaging compatibility

**Self-hosted Telegram and WhatsApp archive for AI agents. A GoSync open-source project.**

Collect messages and attachments once, then let agents search and read them through
MCP or HTTP with separate credentials and account/chat permissions. Original files
use SHA-256 deduplication; references to every source message stay separate.

[Conversation view and compatibility](CONVERSATION.md) explain message kinds,
ordering uncertainty and the last known dialogue turn.

Development version: **0.9.19 — experimental**, PostgreSQL schema 40.

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
Original retention and independent searchable descriptions require explicit
worker configuration; [media storage](MEDIA_STORAGE.md) describes the 30-day
policy and best-effort original recovery. Durable history-wave execution and
cross-occurrence AI artifact reuse remain planned. Optional cloud processing is
off until explicitly configured.

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
Current work, priorities and delivery status live in the existing Tiden product;
[BACKLOG.md](BACKLOG.md) provides navigation and historical IDs. Agents follow the
[Tiden product workflow](docs/operations/tiden-product-workflow.md).
Component responsibilities and verification boundaries: [ARCHITECTURE.md](ARCHITECTURE.md).
Historical agent review context: [REVIEW_FOLLOWUP.md](REVIEW_FOLLOWUP.md).
Optional runtime diagnostics: [TELEMETRY.md](TELEMETRY.md).

## License and security

MIT for original GoSync code. Dependencies retain their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `deploy/MOBY-LICENSE`.
Report vulnerabilities privately through [GitHub security advisories](https://github.com/GoSync-Inc/deedkit/security/advisories/new),
not by posting session data or tokens in an issue. See [SECURITY.md](SECURITY.md).

Platform operations: [profiles, access, backup, cutover and rollback](docs/operations/platform-cutover.md).
Verified gateway/API promotions: [ready-before-route HTTP releases](docs/operations/seamless-release.md).
