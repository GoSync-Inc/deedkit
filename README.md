# DeedKit

**A self-hosted toolkit for AI agents to work with your messages, Android phone,
calendar, recordings and voice calls.**

An agent can find a document in a conversation, read its contents, manage a
calendar event, carry out a task in a phone app, or prepare a voice call with a
specific goal. DeedKit connects those capabilities through a common MCP/HTTP
gateway, with separate credentials and access for each agent.

This is the existing AnyMsg project, expanded into independent modules. Install
the parts you need. Messaging retains its `anymsg` and `messaging_service` names
and compatible account, database and API identities.

## What agents can do

These capabilities are implemented in the experimental development source.
Availability depends on the modules, providers and rights configured in your
installation.

| Module | Capabilities | Guide |
| --- | --- | --- |
| **Messaging** | Read and search Telegram/WhatsApp conversations, people and attachments; send messages and perform supported messenger actions; follow new events and missing replies. | [Operations](MESSENGER_OPERATIONS.md), [search](SEARCH.md), [sending](SENDING.md) |
| **Android** | Give a phone agent a natural-language goal, continue the same conversation, inspect progress and results, use installed apps and handle order-related conversations. | [Android service](services/android/README.md), [BB route](docs/android-bb-conversation.md) |
| **Calendar** | Read Google Calendar and iCloud CalDAV accounts, find shared availability, preview changes and apply event operations with receipts and conflict checks. | [Calendar service](services/calendar/docs/SERVICE.md) |
| **Recordings** | Read Omi and PLAUD transcripts and summaries; optionally keep a private text archive with local search. | [Recording integrations](services/recordings/README.md) |
| **Voice calls** | Prepare a goal, run a voice conversation, collect answers, request owner decisions and retrieve the result; Gemini Live and Grok engines. | [Calls](services/calls/README.md), [readiness](services/calls/GROK.md) |
| **Gateway** | Expose selected module tools through one MCP/REST endpoint, with per-agent credentials and action ceilings. | [Gateway](services/gateway/README.md) |

### Messaging, files and memory

- Multiple Telegram and WhatsApp accounts with independent collection state.
- Full-text and fuzzy search, Russian word forms, people, chat/date/sender
  filters, nearby context, revisions and source references.
- PDF, Office and table extraction, OCR, audio transcription and optional
  image/document descriptions through local or API profiles.
- Separate download, extraction and AI-processing states, searchable passages
  and timestamped transcription segments.
- Durable subscriptions and explicit acknowledgements. Configured wake
  integrations can notify an agent about messages and processing results.
- Encrypted streaming backups, integrity verification and quarantine restore.

See [file indexing](FILE_INDEX.md), [media storage](MEDIA_STORAGE.md),
[enrichment](ENRICHMENT.md), [subscriptions](SUBSCRIPTIONS.md) and
[backup](BACKUP_STREAMING.md).

### Android tasks and ongoing work

The Android service runs on a Mac or Linux host connected to the phone over USB.
A configured `android_task` route sends goals and follow-ups to an existing BB
Codex conversation on that host. Separate native-agent and workflow paths are
also available.

There are navigation hints for Gojek, Grab, Tokopedia and Halodoc, courier-chat
support, and order observation and wake mechanisms. App versions, device access
and the configured route affect what works. Individual operations and synthetic
tests do not establish a complete purchase-to-delivery scenario.

### Voice calls are experimental

Browser rehearsal, Telegram audio and Twilio phone/SIP/WhatsApp Business paths
are implemented. Each channel needs its own configuration. Twilio WhatsApp
calling is separate from the personal WhatsApp archive connector.

Live acceptance of conversation quality, interruptions, latency and audible
completion remains open. Putting the agent's voice into a Gojek/Grab in-app
courier call is separate, unfinished work. Readiness checks and unit tests do not
prove a working end-to-end call.

## Connect an agent

Use the gateway's Streamable HTTP MCP endpoint:

```text
https://your-gateway.example/mcp
Authorization: Bearer <agent gateway key>
```

The gateway also serves `POST /v1/<tool>`. Its JavaScript client supports the
same catalog:

```js
import { DeedKitClient } from 'deedkit';

const deedkit = new DeedKitClient({
  baseUrl: 'https://your-gateway.example',
  token: process.env.DEEDKIT_GATEWAY_KEY,
});
const available = await deedkit.platformCapabilities();
const hits = await deedkit.call('messages_search', { query: 'contract' });
```

See the [DeedKit client and CLI](packages/deedkit/README.md). The gateway uses
private backend credentials; connecting a client does not pair accounts or
enable external actions.

## Install the modules you need

Start with [personal installation](docs/personal-install.md) for your own server,
or [server setup](SETUP.md) and [platform runtime](docs/platform-runtime.md).
Android needs its USB host; calls and recordings have separate provider setup.

For a source checkout with Python 3.13:

```sh
git clone https://github.com/GoSync-Inc/deedkit.git
cd deedkit
uv sync --group dev
uv run anymsg --help
```

Development source is experimental. Published packages can lag its feature set;
check [releasing](RELEASING.md) and the relevant package guide.

## Messaging compatibility

Python `anymsg` includes the messaging server, operator CLI and HTTP client.
The [npm `anymsg` client](packages/npm/README.md) connects directly to a messaging
server. The `deedkit` JavaScript client connects to the common gateway. Existing
AnyMsg integrations remain supported.

## Boundaries and verification

- Use each agent's own scoped key. Sending, calling, pairing and provider-session
  actions require the authorization described in the module guides.
- Archive reads do not mark messages as read in Telegram or WhatsApp. History can
  be partial; an empty search does not prove completeness.
- Local/cloud processing and original retention require explicit configuration.
  Configuration does not prove current provider connectivity or quality.
- The messaging server's tested stack is Linux amd64, Python 3.13 and PostgreSQL
  18. Windows server execution and Docker ARM support are not claimed.
- WhatsApp uses unofficial whatsmeow by default, with an explicit browser
  fallback. Never run two clients against one provider session.

See [architecture](ARCHITECTURE.md), [verification boundaries](docs/quality/platform-acceptance.md),
[access](ACCESS.md), [data policy](docs/DATA-POLICY.md) and
[contributing](CONTRIBUTING.md). Current work lives in the existing Tiden
product; [backlog navigation](BACKLOG.md) links the historical IDs.

## License and security

MIT for original GoSync code. Dependencies retain their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md). Report vulnerabilities privately
through [GitHub security advisories](https://github.com/GoSync-Inc/deedkit/security/advisories/new).
See [SECURITY.md](SECURITY.md).
