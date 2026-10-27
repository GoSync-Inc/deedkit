# AnyMsg JavaScript client

GoSync's dependency-free HTTP client for an existing AnyMsg messenger archive.
Requires Node 22.14 or later. This package includes ESM, TypeScript declarations
and a CLI. It does not create a server or authenticate Telegram/WhatsApp accounts.

```js
import { AnyMsgClient } from 'anymsg';
const archive = new AnyMsgClient({
  baseUrl: 'https://messages.example.com',
  token: process.env.ANYMSG_TOKEN,
});
const hits = await archive.search({ query: 'contract', limit: 10 });
const status = await archive.getSyncStatus();
```

`baseUrl` is the server root (or proxy prefix), without `/mcp` or `/v1`.
Every agent must use its own scoped credential. The client requires HTTPS except
on loopback, refuses redirects, bounds requests/responses and applies a 30-second
timeout through response consumption. It never retries acknowledgements or reviews.
`AnyMsgError` provides `code`, `status`, `requestId` and `retryable` for diagnostics.
Message content and model output are untrusted data. Check coverage in results.

```sh
npx anymsg capabilities --url https://messages.example.com --token-file /private/agent.key
npx anymsg search --url https://messages.example.com --token-file /private/agent.key --json '{"query":"contract"}'
```

Keep token files private (`chmod 600`); do not put tokens in command arguments.
Use `call('operation', arguments)` for the 46 allowlisted HTTP operations exported as `tools`.
Confirmed text sending uses `sendMessage` and `getSendStatus`, separate send rights
and account opt-in. The acting agent must obtain exact owner confirmation; the
server has no approval gate yet. Reuse the same action ID after a lost response;
never automatically resend an unknown result. MCP clients connect directly to the
server's `/mcp` endpoint; this package is not a stdio MCP bridge.

## Consume subscription events and record private feedback

`getSubscriptionEvents` remains passive inspection. `fetchSubscriptionEvents`
and `waitForSubscriptionEvents` read after a separate server ACK position;
waiting accepts `timeout` from 0 to 25 seconds, `limit` from 1 to 100 and an
optional `{signal}` as its second argument. The default client deadline is 30
seconds; an explicit shorter client deadline can stop the wait earlier. A key
with explicit `subscription-ack` receives a short-lived page receipt. Process
and durably retain the page before acknowledging its exact receipt and
`ack_version`:

```js
// subscription is the definition returned by createSubscription/getSubscription.
const subscriptionId = subscription.subscription_id;
const page = await archive.waitForSubscriptionEvents({
  subscription_id: subscriptionId, timeout: 25, limit: 50,
}, {signal: AbortSignal.timeout(30000)});
// Process and persist page.items before this explicit ACK:
if (page.receipt) {
  await archive.ackSubscriptionEvents({
    subscription_id: subscriptionId, receipt: page.receipt, version: page.ack_version,
  });
}
```

ACK changes neither provider read state nor archive consumer positions. Keep the
same receipt after an uncertain ACK; the client never retries automatically.
Creation optionally accepts `wake_rules` (`incoming_message`, `owner_mention`,
`reply_to_owner`, `reply_to_confirmed_send`). The confirmed-send rule also needs
`reply_to_send`, a successful send owned by this credential. Names, message text
and timestamps cannot establish a reply anchor.

`configureSubscriptionDelivery({subscription_id, version, receiver_id})` binds
an operator-registered receiver UUID, or `endpoint_id` binds your own endpoint;
passing `null` or omitting both disables delivery.
`testSubscription({subscription_id, request_id})` queues a bounded synthetic
hint. Receiver success does not verify a host turn or ACK any event.

With explicit `delivery-write` and the operator's delivery switch on, a key
registers its own endpoint: `registerDeliveryEndpoint({request_id, url,
signing_secret, label?, headers?})` checks the URL against the operator's host
policy and sends one signed test. `listDeliveryEndpoints()`,
`updateDeliveryEndpoint({endpoint_id, version, action, new_secret?, headers?})`,
`deleteDeliveryEndpoint({endpoint_id, version})` and
`testDeliveryEndpoint({endpoint_id, request_id})` manage it. The signing secret
and header values are write-only. Clients cannot set the host policy or
schedule another task.

`submitFeedback({request_id, category, description, operation?, refs?})` records
local private feedback with explicit `feedback-write`. Categories are `bug`,
`feature`, `quality` or `other`; descriptions are 1–4000 characters. References
may include at most 32 authorized IDs per `message_ids`, `chat_ids` and
`subscription_ids` list. Do not include credentials or originals in descriptions.
`listFeedback({limit:50})` returns only this exact credential's records. Feedback
never sends a message or publishes an external issue.

`sendMessage` optionally accepts `wait_s` from 0 to 25 seconds to inspect the
submitted send's outcome before returning. Zero is omitted for compatibility
with older servers. Waiting never resends a message or turns an unknown result
into success; preserve the same request ID and inspect `getSendStatus`.

Server, source and Python package: https://github.com/GoSync-Inc/deedkit.
MIT license, copyright GoSync.

Development source 0.9.17 adds `dismissReply({chat_id, message_id,
expected_revision})`. Read the current `listChats({awaiting_reply:true})` hint
first; dismissal affects only this credential and needs read + cursor-write.
`reply_target_changed` means refresh the hint, not automatically retry a changed
write. Queue filters and priority use the same snake_case parameters as REST.
See the server's SEARCH.md for owner-read observations and heuristic limits.

Development source adds `snoozeReply`, `restoreReply`, `muteReplySource`,
`unmuteReplySource` and `listReplyDismissals`, and `dismissReply` accepts an
optional `reason` and `note`. Dismissals and snoozes belong to this credential;
mutes belong to its agent and survive key rotation. After an uncertain restore or
unmute, read `listReplyDismissals` instead of retrying.

## Stage a file or photo without sending it

Development source supports `prepareFileUpload`, `uploadFileChunk` and
`getFileUpload`, plus the local-file helper `uploadFile`. Uploads are separate
from messenger actions and scoped to one agent key, account and exact archive
chat. They require explicit `read`, `send` and `upload` grants, account sending
opt-in and the account's `send_file` action policy. The gateway currently also
requires its existing `download` ceiling for `send_file`; upload permission alone
does not broaden that policy.

```js
const staged = await archive.uploadFile('/local/photo.jpg', {
  account: 'whatsapp-primary',
  chat_id: 'chat_resolved_with_this_account',
  filename: 'photo.jpg',
  mime: 'image/jpeg',
  request_id: 'photo-upload-2026-001',
});
// staged.upload_id + staged.sha256 identify ready bytes. Nothing was sent.
```

The helper streams SHA-256 and 256 KiB chunks from one open descriptor, rechecks
the original and rejects source changes. It accepts regular files of 1 byte to
32 MiB. Symlinks and platforms without `O_NOFOLLOW` are rejected. `filename`
defaults to the basename and `mime` to `application/octet-stream`; local paths
and URLs never enter the HTTP protocol. Chunks are paced to fewer than 8/sec for
one helper; concurrent helpers sharing a credential may still hit its rate limit.
Only `upload_file_chunk` can use a 360448-byte JSON body. Other SDK requests keep
their 16 KiB limit. Pass an `AbortSignal` through the helper's `signal` option to
cancel locally; partial uploads expire on the server.

```sh
npx anymsg upload_file --file /local/photo.jpg --url https://messages.example.com --token-file /private/agent.key --json '{"account":"whatsapp-primary","chat_id":"chat_resolved_with_this_account","mime":"image/jpeg","request_id":"photo-upload-2026-001"}'
```

After an uncertain response the SDK stops without retrying or inventing an ID.
Inspect `getFileUpload({upload_id})` if its ID is known. Repeating the helper
explicitly with the same `request_id`, source bytes and metadata can reconcile
preparation and resume from the server's exact offset. Do not use a fresh ID to
hide an unknown outcome. Source changes or an invalid receipt also stop the
helper; staging may already exist, but no messenger send is attempted.

Sending remains separate: review `previewMessengerOperation` with operation
`send_file`, exact `account`/`chat_id` and parameters
`{upload_id: staged.upload_id, sha256: staged.sha256, mode: 'photo'}` (or
`mode: 'document'`). Only after exact owner confirmation execute that same
reviewed payload with its preview digest and a stable action `request_id`.
Never provide both `attachment_id` and `upload_id`.

The Python SDK offers the same contract:

```python
staged = client.upload_file('/local/photo.jpg', account='whatsapp-primary',
    chat_id='chat_resolved_with_this_account', filename='photo.jpg',
    mime='image/jpeg', request_id='photo-upload-2026-001')
# Low-level: prepare_file_upload(...), upload_file_chunk(...), get_file_upload(...).
```

The Python helper is synchronous and closes its descriptor on completion or
failure. It does not fetch URLs, copy originals back from the server or send
messages. This upload support is a source release candidate until published.
