# One conversation across an order's apps and participants

Use the existing ordinary-native android_agent conversation for a goal-bound
Halodoc consultation/pharmacy/delivery or Tokopedia seller/dispatch/tracking task.
The legacy android_courier_chat Gojek/Grab enum describes that compatibility tool.
It does not define the ordinary agent's generic apps field or OrderControl's
package support. Caller capability descriptions and sending authority remain
separate root/A work; this source does not broaden legacy APPS or grant a send.

The native agent chooses what to inspect, ask, respond to, schedule and notify.
The registered android_order_control bind/update/next_check/notify/handoff
operations record metadata in the existing journal. Grok fronts the conversation
through existing authenticated wake fetch, read_receipt, original native reply
and explicit handoff. There is no Halodoc/Tokopedia engine, app classifier, UI
state machine, skill, parsing rule or message template.

Preserve the exact host-stored owner_instruction and original task/request/thread
correlation. Before an order exists, provider_order_id stays absent; a doctor
question or seller clarification still has a task, original item/message and full
async question array. Do not manufacture RPC/question IDs. Later observed order,
consultation, participant, physician-artifact and tracking references belong in
flexible state with their evidence. The transport preserves these fields; it does
not certify clinical truth, participant identity or permission.

For Halodoc, unknown clinical facts return to the owner. A physician-issued
artifact may be retained with its observed issuer/reference; the agent must not
invent symptoms, consent, diagnosis or prescriptions. For either app, a material
basket/price change returns for confirmation of the complete changed basket.
Normal coordination within the actual delegated order remains the agent's
responsibility under the owner's applicable authority. Neither notification text,
a participant's claim, a read receipt nor transport HTTP200 answers these questions.

A question emitted after bind can occupy a separate task-notice obligation.
Handoff snapshots now retain questions from that same authenticated owner,
device, original task, request and thread. Each keeps its original obligation,
nullable provider reference and native identities; it is explicitly a task notice
rather than a guessed order association. Other tasks, requests, threads, devices
and separate bound orders remain excluded. Relevant sending/unknown notices block
proposal. A new question or changed delivery state changes the snapshot and
invalidates a previously read proposal. Old unresolved records remain held.

Codex retains responsibility until the intended authenticated Grok principal
separately accepts the exact current responsibility snapshot and future next
check. Fetch is a read; read_receipt acknowledges local metadata delivery; neither
transfers responsibility or confirms native input/goal completion. A new foreground
task preserves earlier obligations. No push uses the chosen persisted timer; no
live retained lease produces the existing visible supervision hold. Unknown
delivery keeps its original wake/request identity across restart and cannot be
bypassed with a new proposal or ID. Actual ambiguous chat sends additionally need
their original native/provider receipt reconciliation; these fixtures do not
simulate or prove a real send.

An async task-notice question settles only through the host-only
`OrderControl.settle_answered_question(principal, owner, device_id, wake_id, receipt)`.
Its `receipt` is the host's own dialogue `CodexSession.receipt()` of that original
async_question: same request/thread/turn/item, an answer message and native
`accepted` (steer acknowledged or turn/start receipt). Accepted means the answer entered
the conversation, never goal, order or action success. The wake becomes `done` under
CAS with original IDs, payload and export intact. The receipt evidence is stored once:
a duplicate reads back and a different answer conflicts. When no unanswered question,
unread status or uncertain delivery remains, the task notice becomes `settled`. A later
notice or bind from the same exact task reopens that record. RPC questions (no positive
contents ACK), bound orders, pending/unknown/refused answers, sending/unknown deliveries,
foreign principals/devices, revoked rights and another source epoch stay held. Neither a
read receipt, expiry, idleness nor a finished run settles a question. A caller cannot
supply this receipt.

The host calls it in four places. Service boot, after process recovery and before any
native session, supervision or route, reconciles each still pending/accepted question
of an active task-only notice that has no settlement yet (at most 64 per boot). The host
acts as the wake's own recorded principal, re-checked against current config as the
journal's existing claim and export do; no caller, forged header, native object,
dispatch, replay or new ID. Only a settled outcome adds one `order_settlement` run event
with `reconciled: restart`; a hold changes nothing and the next boot or an authorized read
retries the same receipt. An async `reply` that native steer accepts returns
`order_settlement`. A resumed answer records an `order_settlement` run event after the
turn/start receipt. An authorized `read_receipt` of an async question reconciles from
the durable `.dialogue.json` row. That journal path binds owner/device, and the row binds
request/thread/turn/item. The obligation's checks bind source and epoch. Fetch and
status stay read-only. A failed settlement never changes the accepted input receipt,
resends or mints an ID: it reports `held` with a reason, and the same persisted receipt
reconciles later. A missing, duplicated, mismatched or non-accepted row stays held. An
already settled question still records a later read.

The gateway's source page (`OrderFeed.read_source`) is a view of the immutable feed. A row
whose stored `expires_at` has passed on the source clock, or whose wake is done, cancelled
or refused, is skipped. The server would refuse it as `android_wake_expired` or must not
send it. Skipping keeps a later eligible wake from being stranded behind it. The row, its
original IDs, expiry and sequence stay unchanged. The page keeps the server cursor as
`after`, and `next_after` is the last returned sequence, so sequences may be sparse. A page
of only retired rows is empty: no receipt, stage or delivery happens, and the cursor stays.
Every scanned row is still authorized first. A revoked order, malformed metadata, or
another owner, device, source or epoch fails closed (`access_changed`,
`invalid_feed_metadata`) and is never skipped. Pending, sending, unknown and accepted
wakes stay eligible. The server keeps refusing raw expired input.

The focused conversation tests use real OrderControl, OrderFeed, OrderEvents and
Supervision with synthetic UI observations and identifiers. They verify context,
question, scope, timer, uncertainty and responsibility transport, including the
bind-before-question regression. They do not run a native model, doctor, provider,
phone or real application, prove an agent's clinical decisions, or measure speed.

PR962 and proposal/2026-11-12-halodoc-order / ANYM-994 remain separate and held.
That historical skill/WhatsApp plan is not merged, rewritten or silently adopted.
ANYM-997 covers this generic journal correction; ANYM-993 supplies the ordinary
native conversation, and ANYM-978 is the earlier courier delegation dependency.
Root/A must separately resolve caller description/authority and actual native,
doctor/real-app and order-bound acceptance. No live success is claimed here.
