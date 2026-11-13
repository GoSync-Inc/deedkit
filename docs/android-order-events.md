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
Questions accept only `item/tool/requestUserInput`; `answer(...,answers={original_question_id:
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
