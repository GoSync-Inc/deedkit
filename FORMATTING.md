# Message formatting and preview

Status: **implemented in 0.9.14**, PostgreSQL schema24. Plain remains the default.
Formatting does not change [SENDING.md](SENDING.md) owner approvals. Native visual
rendering is not proven by synthetic tests; live acceptance needs separately
approved messages for each provider.

## API and persistence

`preview_message(account, chat_id, text, reply_to="", format="plain",
link_preview=false, allow_lossy=false)` requires `read`. It resolves only an
authorized archived target, makes no provider call and queues nothing. A
disconnected collector or disabled sending does not prevent preview. The REST
endpoint is `POST /v1/preview_message`; Python provides `preview_message` and npm provides `previewMessage`.

The response contains source and visible `rendered_text`, wire `provider_text`,
UTF-16 semantic entities, provider payload, safe warnings, visible link
destinations, wire `utf16_units`, renderer version, target/reply and effective
`render_policy`, plus `preview_digest`. The digest binds the principal and scope
version, account/chat/provider target, reply, exact source and complete render
artifact/policy. It is a consistency check, not an owner approval credential.

`send_message` accepts the same fields plus `request_id` and `preview_digest`.
Rich text or a nondefault link/loss policy requires the exact reviewed digest;
missing or stale previews return `preview_required` or `preview_mismatch`. Plain
with default policy retains the earlier request shape and idempotency behavior.
The acting agent still shows account, recipient, visible text, formatting,
warnings, destinations and reply to the owner and obtains approval before sending.

Migration24 preserves legacy plain rows. New rows persist source, format, full
render artifact, policy and optional digest; the existing outbox body stores
provider wire text for receipt/provenance comparisons. Identical retries compare
saved intent before invoking the current renderer. Dispatch validates and uses
the saved payload without rendering it again, including after parser upgrades
or restarts. Status exposes only safe format/version/warning/digest metadata.
Draft text, entities and URLs are not written into audit entries.

The renderer pins `markdown-it-py==4.2.0` with raw HTML disabled. Bounds are
16000 source UTF-16 units, 4000 provider units, 1024 parser tokens, 128 entities
and 32 nesting levels. The existing HTTP JSON body limit can further restrict
large source markup. Invalid markup, unsafe links, unsupported nesting and
complexity are rejected before queuing. Nothing is fetched while rendering.

Incoming `formatting` is separate evidence bound to the exact archive revision.
Telegram native entities may be observed, partial or unknown. Unsupported native
entities are reported without manufacturing mentions/custom-emoji identities.
WhatsApp records `wire_observed` and a SHA-256 of captured wire text; it does not
claim that guessed markup yields complete native entities. Absent capture is
unknown, distinct from observed empty entities. Compact unknown evidence carries
only its state; full evidence retains its version and diagnostic fields. First known evidence appends a
revision; equal-time contradictory known evidence remains a conflict, and later
unknown captures cannot erase known evidence. Existing snapshots stay immutable.
Offsets always reference the full provider body in UTF-16 units, including
compact or truncated responses; `offsets_reference` states this explicitly.

When only formatting changes, completed OCR/transcription/model results remain
available for unchanged originals. Reuse requires the same proven message input,
current file digest and previous source revision; passage indexes are rebound
without extracting or indexing the same text again. Changed content, deletion,
stale evidence or interrupted leases still invalidate the old result.

## Input dialect

Markdown starts with CommonMark paragraphs, soft/hard breaks, `**bold**`, `_italic_`,
inline/fenced code, block quotes, ordered/unordered lists and links. Enable
strikethrough `~~text~~` and spoiler `||text||` explicitly; they are extensions,
not assumptions about the baseline parser. Preserve literal punctuation such
as `5*3`, prices and unmatched delimiters. Define heading/list rendering and
paragraph breaks in fixtures so profile upgrades cannot silently change sends.

HTML is a **restricted message-markup dialect**, not a web page: allow only
documented text styling, code/pre, quotes, breaks and links. Reject unsupported
tags/attributes rather than executing or fetching them. Scripts, event handlers,
images, embedded media, styles and remote includes are not message formatting.
Limit input length, token/entity count and nesting depth in both modes.

Initial rich-link destinations allow HTTP(S) only, without fetching them during
preview or rendering. Reject executable/local schemes and hidden control
characters. Preserve URL destinations and make them visible in the approval
preview. Provider-native mentions/custom emoji and other special actions require
separate explicit support, rather than constructing them from arbitrary HTML. Mentions
of group members have it: the `mentions` parameter (SENDING.md, ANYM-928), whose
`{sender, offset, length}` spans count UTF-16 code units of the source text like
these entities. A mention span must not overlap a link or code span; inside a style
span it may. Telegram sends it as `InputMessageEntityMentionName` beside the
formatting entities; WhatsApp replaces it with `@<number>` in the wire text.

## Provider behavior

| Feature | Telegram | WhatsApp |
| --- | --- | --- |
| Bold, italic, strike | Explicit native entities | Native markup with delimiter validation |
| Inline/fenced code | `code`/`pre`, preserving language when supported | Supported native code syntax; warn on unsupported language styling |
| Quotes and lists | Quote entities; textual list markers | Native quotes/list markers with bounded textual markers |
| Named links | Text-link entity plus destination in preview | Visible `label: URL`, with a conversion warning |
| Underline | Native underline entity | Unsupported: explicit loss warning before approval |
| Spoiler | Native spoiler entity | Unsupported: reject rich send unless its visible-text conversion was explicitly previewed and accepted |
| Expandable quote | Unsupported in this release | Unsupported; reject |

Telegram sends use explicit entities with `parse_mode=None`. Offsets and lengths
count **UTF-16 code units**, including emoji/surrogate-pair boundaries. The
[Telegram entity specification](https://core.telegram.org/api/entities) also
restricts nesting for some entity types; accepting a parser tree does not prove
that every combination is a valid provider payload. Validate and test the
supported combinations before queuing.

WhatsApp remains on the existing whatsapp-web.js collector and private sending
lane. Do not open another session. Do not claim native markup is disabled by the
current `plain` input mode: it passes text through to the provider. Literal
delimiter behavior needs provider acceptance tests, not invented escaping rules.

Link previews remain off by default. Add an opt-in only with explicit preview
policy in the payload digest and documentation of provider limitations. The
current library documents that its `linkPreview` option has no effect on
multi-device accounts ([send options](https://docs.wwebjs.dev/global.html#MessageSendOptions));
do not promise that a boolean always controls provider behavior.

## Length, evidence and retries

- Keep the current conservative 4000 UTF-16-unit send limit. Validate the rendered provider payload,
  including any expanded WhatsApp links/markers, rather than source length alone.
- **No automatic splitting.** Return a useful length error and a useful source/payload distinction. Multipart sending needs its own approved ordered
  payload, per-part idempotency/status and partial-failure semantics first.
- Receipts and identity repair compare the observed provider text with the
  persisted provider payload, not the unrendered Markdown/HTML source. Extend
  agent provenance accordingly; rich text must not break confirmed sender repair.
- Keep source text and render artifacts scoped like the outbox. Status/diagnostic
  responses must not suddenly echo full outgoing bodies, keys or request hashes
  to other readers. An authorized preview deliberately contains its draft.
- Audit identities, action IDs and safe warning codes; do not log draft bodies.
  Quarantine restore cancels queued rich sends and marks interrupted attempts
  unknown, just as it does plain sends. Never retry an uncertain provider result.

## Reading formatting back

`get_messages`, `get_context` and `search` accept `body_format=html` (ANYM-737; empty or absent adds nothing, and
`detail=transcript` refuses it with `invalid_parameters`). A Telegram row whose formatting is known then carries
`body_rendered = {format, text, lossless, omitted_types}`: the complete provider body (even when `body_chars` cut
`body`) as HTML that `preview_message` and `send_message` accept as `text` with `format=html`.

- **Verified, not trusted.** The server runs the sender's own `render_message('telegram', text, 'html')` on the generated
  text and returns it only when the visible text equals the stored body and the entities equal the stored supported
  entities. Anything else gives `body_rendered_unavailable` with one of `unknown_formatting`, `no_entities` (WhatsApp),
  `partial_evidence`, `evidence_mismatch`, `too_long`, `not_reproducible`, `response_budget` or `render_failed`. Visible
  text is never changed to make a row pass.
- **Lossless.** `bold`, `italic`, `underline`, `strikethrough`, `spoiler`, `code`, `pre` (with `language`),
  `blockquote` and `text_link` render; Telegram's implicit entities (`url`, `email`, `phone`, `mention`, `hashtag`,
  `cashtag`, `bot_command`) stay plain text. `expandable_blockquote`, `mention_name`, `custom_emoji`, unknown types and a
  link the sender's validator rejects are left out and listed in `omitted_types`; `lossless` is then `false`.
- **Dialect limits.** The sender treats `blockquote` and `pre` as blocks joined by a blank line, and counts its root in the
  32-level depth cap (31 nested spans). A layout that needs different separators, a span ending in whitespace, crossing
  spans or nested code is `not_reproducible`.
- **Escaping.** `&`, `<`, `>` are escaped in text and in `code`/`pre`; an `href` is double-quoted with `&`, `"`, `<`, `>` escaped.
- **Bounds.** One conversion per unique (message, revision); every serialized `body_rendered` (a repeated anchor too) counts, the
  total in one response is at most 256 KiB and later rows get `response_budget`. The text must also fit a preview or send request
  (16 KiB HTTP body): a text over 15,100 bytes as JSON UTF-8 is `too_long`. Nothing is sent to a third party, logged or stored; the agent still shows the
  owner the text and format before a send (SENDING.md).

## Acceptance

Use synthetic fixtures for Cyrillic, emoji before/inside spans, nested styles,
code containing delimiters, malformed markup, `5*3`, balanced URL parentheses,
lists/quotes, unsafe links/HTML and every unsupported conversion. Test renderer
bounds and UTF-16 offsets, including rejection of split-surrogate offsets.

Then test strict MCP/REST argument parity, installed Python/npm clients,
preview-to-send binding, renderer upgrades/restarts, idempotency conflicts,
ACL/revocation/expiry, reply targeting, source/provenance comparisons,
formatting-only revisions and encrypted backup/quarantine restore. Ordinary
tests must not invoke live providers. Live acceptance uses separately approved
messages for each provider; do not infer visual rendering from mocks alone.
