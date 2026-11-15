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
