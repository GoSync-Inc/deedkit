# Android order event seam

Source modules only: core/API/native/gateway integration belongs to DK-6. No phone listener,
forward, notification access or Grok receiver is installed or proven by this change.

`order_events.py` stores obligations, observation references and wake intents in the existing
private SQLite connection. Initialize under Store's process lock; call `recover()` once at
service boot. It changes interrupted `sending` to `unknown`. Never call recovery beside a live sender.
Use the existing private state directory, permissions and backup policy; no raw push text or secret
belongs in these records. No new dispatcher service or LLM polling loop is needed.

## Core integration contract

```python
events = OrderEvents(store.db, authorize, clock=time.time)
monitor = OrderMonitor(events, submit=core_submit, grok_submit=verified_grok_submit)
# authorize(principal, owner, device_id, order_id, source_id, auth_epoch) -> bool
# async submit(owner, device_id, original_request_id, *,
#              native_thread_id, native_turn_id, kind, payload) -> receipt
```

Authenticate the source/callback before entering this internal module. `authorize` fails closed
and rechecks current principal, device, order, source and access epoch at admission and delivery.
A must keep its existing device lock, reservation renewal, same native thread and safe-boundary
admission around `submit`. The module never acquires another device lease or native client.
`device_busy` is a definite refusal before native delivery; keep the intent pending until a safe
boundary. A newly arriving task leaves every older obligation intact.

Bind each approved active order through `bind(principal, owner=..., device_id=..., order_id=...,
package=..., source_id=..., source_order_id=..., auth_epoch=..., original_request_id=...,
native_thread_id=..., original_user_words=..., next_check_at=...)`. Words remain verbatim.
Native turn/question IDs are optional binding fields. Rebinding cannot replace existing duties.
The state document and next check are agent decisions, without an order workflow or text classifier.

`observe(principal, Notification(...))` validates exact bindings and timestamps, deduplicates source
event IDs and coalesces pending wakes. With `order_id=None, source_order_id=None`, only active
orders of that exact owner/device/source/package get an explicitly `unresolved_app` observation.
This is an invitation to inspect candidate orders, never an inferred order status or authority.
Unrelated applications are excluded. Notifications never invoke reply/approval APIs.

The existing lifecycle calls bounded `stage_due()` and `dispatch_once(wake_id)` on due timers and
committed observations. Use one host timer for the earliest persisted next check; reload after
restart. This waits without a model and works when pushes are absent. Missing receiver/access and
unknown delivery holds appear in authorized `order(...).delivery`; expose them to the caller.

Receipt fields are `status: accepted|unknown|refused`, `original_request_id`, `native_thread_id`,
`native_turn_id`, `question_id`; question/answer receipts also retain `native_request_id` and
`item_id`. Preserve the JSON number/string type of native RPC IDs. `accepted` confirms input
delivery only. `sending` commits before any downstream await. A timeout, cancellation, malformed
receipt or lost reply becomes `unknown`, never resend/new-ID. `reconcile(principal,wake_id,receipt)`
requires positive authenticated acceptance evidence. It cannot infer non-delivery from silence.

After processing an accepted observation, `checked(principal,wake_id,next_check_at=...,state=...)`
records the agent's next check and releases that batch. Later observations remain pending.
`lifecycle='received'|'cancelled', next_check_at=None` ends timers; an incoming push cannot close it.

`notify(principal,owner,order_id,notice_id=...,kind='question'|'status',native_turn_id=...,
question_id=...,native_request_id=...,item_id=...,payload=...)` queues nonsecret caller information.
This legacy entrypoint accepts only `item/tool/requestUserInput`; `answer(...,answers={original_question_id:
[string,...]})` retains every native ID and creates one durable reply. A validates the still-live
RPC/turn before submission; this helper cannot answer approval RPCs or handle protected input.

`propose_handoff(...,target_principal=...)` sends original words, current state, pending positions
and next check. Codex remains responsible until `ack_handoff(target,wake_id,snapshot_digest=...)`
authenticates an explicit acknowledgment of the exact unchanged snapshot. A new observation or
check makes an old acknowledgment stale. Timers and pending wakes switch atomically to Grok;
unknown delivery blocks transfer. No second independent monitor starts.

## Notification source and external prerequisites

The new `services/android/notification_listener/` companion uses Android's
[NotificationListenerService](https://developer.android.com/reference/android/service/notification/NotificationListenerService).
It exports package, time and hashed notification key only, through a durable bounded journal.
It filters to USB-configured active packages before journaling; source text never leaves the app.

The stream is an abstract local socket, `deedkit-order-events`, restricted using
[LocalSocket peer credentials](https://developer.android.com/reference/android/net/LocalSocket#getPeerCredentials())
to shell/root. Root must prove the existing authorized USB ADB forward's peer identity and
reachability; SELinux/OEM refusal remains a visible blocker. No public listener, phone tailnet,
new device key, INTERNET or Accessibility permission is introduced. Existing ADB authorization
is required; this source does not pair devices.

After root installs/grants access in an allocated window, A owns the serial-pinned forward and
passes its local stream to `UsbOrderSource.read_stream`. Watch request: `{version:1, packages:[...],
source_epoch, after}`. Frames are `hello`, `observation`, `heartbeat`; epoch changes and bounded
journal gaps remain visible holds. EOF keeps next-check fallback. This is event-driven socket
streaming, not ADB notification polling. Installed delivery remains unproven until root verifies it.

Messaging's signed-hint/Standard Webhooks delivery and wake-relay already provide event identity,
access fences and ambiguous-delivery rules. They cannot consume Android notifications directly;
ANYM-844's Codex queue adapter is not the Android native app-server session. Reuse configured
messaging delivery for suitable caller hints after its real receipt path is verified.

Root/admin must obtain the released Grok technical client's actual callback/receipt contract:
verified wake destination, accepted receipt lookup by immutable delivery identity, authenticated
question answers, and explicit responsibility acknowledgment with snapshot digest/next check.
Current `grokAutomation` source proves only configurable endpoint and HTTP acceptance, not these
capabilities. Supply an adapter to this contract or implement that remote client protocol;
no URL or credential is invented here. Root owns integrated source/main/B and live acceptance.


## Native task control before a provider order exists

`order_events_control.NativeOrderContext` carries A's server-injected current Principal, owner,
device, task/request, native thread/turn, actual call ID and typed JSONRPC ID, optional observed
item and namespace. Task/request have distinct meanings and may hold the same actual run ID.
There is no synthetic ID to force them apart. Principal authentication hashes are never serialized.
A validates the exact registered `android_order_control` tool, namespace/operation, current rights,
native call and device gate. These Python methods do not register a model-callable tool themselves.

Construct `OrderControl(events, source_id=..., auth_epoch=..., original_words=...)` with trusted
host source/epoch and a synchronous callback returning A's exact stored `owner_instruction`.
The immutable goal and typed nonsecret context remain in A's existing task store, resolved by the
original task/request; Grok never reconstructs them. Model arguments cannot replace words or authority. `await control(context, operation, payload)`
accepts exactly five operations:

- `bind`: optional actual package/provider/source-order references, agent-chosen state and future
  `next_check_at`. The returned `obligation_ref` is explicitly internal; it is never a provider ID.
- `update`: `{obligation_ref, changes}` updates state/provider metadata/lifecycle; empty changes
  inspect a snapshot. Without a reference, empty changes discover a bounded active list. A new task
  may address earlier duties of the same authorized owner/device/thread; origins remain unchanged.
- `next_check`: `{obligation_ref, at, checked_wake_id?}` persists the model's future timestamp. Existing bounded timer
  admission uses it after restart. Supplying the original accepted observation as `checked_wake_id`
  explicitly records the completed check and rearms delivery; unknown wakes cannot be settled this way.
  Later observations remain pending. Received/cancelled state stops the timer and retains unknown tombstones.
- `notify`: `{obligation_ref?, message}` queues model-chosen nonsecret owner status, including before
  checkout. A notice-only task record has no automatic monitoring timer or fabricated provider ID.
- `handoff`: `{obligation_ref, target_principal}` proposes the existing responsibility snapshot.
  Codex stays responsible until an authenticated target confirms its unchanged state and next check.
  The snapshot retains pending questions and their original array without asserting approval or native
  acceptance. `{obligation_ref, withdraw_wake_id}` explicitly withdraws a known unswitched proposal;
  sending/unknown delivery must first be reconciled and cannot be withdrawn or bypassed with a new ID.

Receipts say `effect=metadata_only`; queued notices report pending delivery separately. One actual
native thread/turn/call has one durable operation receipt. Changing its typed RPC ID, arguments or
origin cannot create a second obligation/wake. Repeated reads recheck original source authorization;
a new source epoch cannot reauthorize cached older data. All operations reuse the original SQLite
obligations/wakes, preserving legacy pending/unknown records without another monitor.

`observe_native_question(context, item_id=..., message_id=..., questions=...,
native_request_id=None, question_ids=[])` retains the actual default-mode async item/message and
whole question array, including nullable options. Missing RPC/question IDs remain absent, not
invented. Genuine RPC questions retain their typed ID and all original question IDs. Ordinary async
clarification never becomes an approval or secret-input RPC. Protected tool requests are handled
separately by A/C; their target/purpose/reference/completion never enter this helper or hint.

`OrderFeed(control).read_source(principal, owner, device_id, after, limit)` returns
`{journal_id,after,next_after,events,gap}` for the existing gateway bridge. Exports have durable
owner/device sequences, immutable original wake IDs/metadata/deadlines and authorized full-content
`fetch`. Read/fetch does not retire a wake. Freezing an observation preserves its through-position;
later observations remain for the next batch. Source hint expiry does not cancel an obligation or
change its chosen next check.

The merged server producer uses the Android intent itself as outbox in the existing endpoint lanes,
with actual backend principal/key and an explicit root binding. No message subscription is borrowed
and no message/module cursor is advanced. Only minimal reference metadata reaches its signed hint;
full words/questions/state stay on authenticated Android fetch. Missing/unverified binding is
unavailable. HTTP200 only accepts transport. `ack_read` records authenticated Grok read separately
from native input/goal evidence; `ack_handoff` additionally requires the unchanged snapshot digest
and confirmed future next check. No HTTP response or read ACK transfers responsibility by itself.

A owns actual dynamic-tool registration, host lifecycle/device gate, public same-tool fetch/reply/
ack/handoff schemas and gateway wiring. Those interface names are reserved and are not claimed live
by this helper. Root owns actual routine instruction/body handling, binding/readiness, installed
listener/USB grant and same-conversation live proof. Native input submitted/unknown/resolved, an
explicit matching input receipt, Grok read, responsibility ACK and HTTP acceptance remain distinct.
No generic native RPC forwarding, provider action, new daemon or model polling loop is introduced.
