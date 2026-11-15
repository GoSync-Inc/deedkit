# One Halodoc order, Android and WhatsApp

ANYM-994; prototype, no live order. Grok holds one flexible order document in its
existing caller conversation. The phone worker executes bounded Android goals and
returns observations into it; native messaging handles associated WhatsApp replies.
No new workflow service, database, state machine, grant or data sink is needed by
the experiment. This does not establish live caller-path completion or speedup.

## Official flow evidence, 2026-11-12

Halodoc's [checkout FAQ](https://www.halodoc.com/faq/questions/kenapa-saya-diarahkan-ke-konsultasi-dengan-dokter-saat-ingin-membayar-obat)
says prescription medicines need a doctor's prescription and an online consultation
can follow medicine payment. Its [doctor terms](https://www.halodoc.com/syaratdanketentuandokter)
state that the physician issues an eRx when their assessment permits the medicine;
the [medicine refusal FAQ](https://www.halodoc.com/faq/questions/mengapa-dokter-tidak-meresepkan-salah-satu-atau-beberapa-obat-obatan-yang-telah-saya-pesan)
lists clinical mismatch and incomplete answers among reasons for refusal. These are
service contracts, not verified Android labels or a reason to force every order through
one sequence. [User terms](https://www.halodoc.com/syarat-dan-ketentuan) describe third-party
delivery and communication between delivery and user. The owner's report of associated
WhatsApp messages motivates this handoff; these sources do not prove that every medicine
delivery contacts the patient on WhatsApp or that a particular sender is legitimate.

## Authority and ownership

Owner 2026-11-12, relayed by orchestrator @thread:thr_h748zsv2mq:
«Холодок тоже нужно. Там ещё сценарий с тем, что в холодке надо учесть, что они заказ,
потом врач открывает, ты с ним должен ему обосновать, что тебе зачем нужно.
Он выписывает рецепт, и потом уже всякие уже с курьером, не курьером и так далее.
Они часто пишут в WhatsApp ещё. Это надо сценарий тоже связать, чтобы игрок уже на WhatsApp отвечал.»

Normalized names: Halodoc; outer caller Grok. AGENTS.md/SENDING.md apply this request
to one **actual recorded owner order goal**, with its bound doctor/pharmacy/delivery
participants. They do not turn ANYM-978's Gojek/Grab courier exception into arbitrary
WhatsApp authority. The current work has no actual medication/order goal and authorizes
no live consultation, order or send. Calls, ACK, sessions, pairing, auth/access and the
five reserved decisions retain their existing rules. No reviews apply.

Android worker @thread:thr_htz7gqdcw6 exclusively owns services/android, phone, mini
and phone CODEX_HOME. It confirmed installed package `com.linkdokter.halodoc.android`;
no Halodoc screen or deep link has been exercised. The staged outer-caller skill is
[halodoc-order](../../skills/halodoc-order/SKILL.md); installation is a separate
coordinated handoff, not part of this prototype. Do not put patient context in phone
navigation skills or global settings.

## Existing contracts and gaps

| Part | Relevant evidence | Practical consequence |
| --- | --- | --- |
| Android goals | ANYM-993; catalog `android_agent`; agent_bare.py:37 | start/get with original request_id, explicit outputs; check missing_outputs and actual data |
| Courier autonomy | Full ANYM-978; courier_goal.py | Gojek/Grab only; order_seen is recorded, not identity enforcement; do not reuse it for Halodoc |
| Native WhatsApp | Full ANYM-258; whatsapp/whatsmeow/send.go:144 | Existing authenticated client, quoted target, policy checks, native receipt; browser ANYM-190/192 do not prove native parity |
| Sending | outgoing.py:127,594; SENDING.md | Exact account/chat/reply target, idempotent request, inspect receipt; no order-level server permission |
| Subscriptions | SUBSCRIPTIONS.md, Available API / Agent wake rules | Known chat/sender filters and opaque hints; labels are routing, never task authority; creation alone does not prove a Grok wake |
| Context continuity | Caller conversation + skill | No existing tool attaches an Android goal to a WhatsApp order. Caller retains correlation; no backend addition demonstrated necessary |

Read-only `product-context` lookup was followed by a complete spec list scan and full
978/258 fetches. Halodoc appeared in unrelated OCR ANYM-981; this is bounded evidence,
not a claim that semantic retrieval proves the absence of any spec. New ANYM-994 is
on `proposal/2026-11-12-halodoc-order`, Platform Gateway component
`d593e609-984f-4f0e-b434-7f78373058a6`; 993/991 were not changed.

## Executable caller recipe

Start with [the synthetic order context](../experiments/halodoc-order/world.json),
replacing its fixture fields only on an actual owner order. Keep owner words, patient
reference, fact/source pairs, unknowns, consultation/prescription/order references,
participant bindings and their evidence, request/send IDs, subscription cursor and
cancellation in the existing caller conversation. One patient/order remains the same
across handoffs; loss of context requires reloading the evidence before continuing.

Pass the needed slice through `android_agent(action="start", request_id=..., goal=...,
owner_instruction=..., device_id=..., session_id=..., model="gpt-6.1-terra",
effort="medium", outputs=[...])` through the phone owner. Inspect with `action="get"`
and the same request_id. Request `consultation_ref`, `needs_owner`, then `prescription`,
`order_ref`, `participant_evidence`, `delivery` as appropriate. A known clinical fact
can answer the doctor's factual question; unknowns return verbatim to the owner.
Only a physician issues the prescription, which may differ from or refuse the request.

Bind WhatsApp account/chat/sender from authenticated order details or explicit owner
identification, then use `messages_get_context(message_id=...)`. Incoming claims,
display names and matching order text alone are insufficient. In a shared pharmacy
chat, another order remains outside this goal. Reply using
`messages_send_message(account=..., chat_id=..., reply_to=<archive incoming ID>,
text=..., request_id=...)`; retain the ID before sending. Use
`messages_get_send_status(request_id=...)` after a lost response, or `send_id` when
known. `sent` is provider acceptance; recipient delivery needs separate evidence.
`unknown` remains unresolved: inspect, report, never automatically resend or replace
the ID. Owner cancellation stops new replies and reports unresolved sends; it cannot
recall a WhatsApp dispatch.

Reuse an authorized subscription, or use `messages_create_subscription` with explicit
chat_ids/sender_ids, incoming message.created and an opaque label. Inspect
`messages_get_subscription_events(subscription_id=..., after=..., detail="compact")`
without ACK. Keep the subscription-to-goal mapping in the caller. Existing configured
Grok wakes may resume that caller; no endpoint/wake configuration was tested or changed.

DATA-POLICY already covers the owner's messenger archive and scoped agent reads;
the Android executor sends its goal/phone content to OpenAI. This prototype uses only
synthetic data and adds no retention/egress. In an actual order, the owner supplies
the clinical facts for that doctor; logistics gets only needed delivery facts. Do not
silently add a health-history store, enrichment recipient or patient-data webhook.

## Functional evidence and limits

Run the author-driven schema/tool exercises:

```sh
python3 scripts/experiments/halodoc_order.py docs/experiments/halodoc-order/baseline-transcript.json
python3 scripts/experiments/halodoc_order.py docs/experiments/halodoc-order/skill-transcript.json --negative-controls
python3 /home/egor/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/halodoc-order
```

Both traces use 16 fake tool calls and two synthetic sends. Baseline has three synthetic
owner questions; scoped authority/context has one (unknown allergy history). This is
an author-chosen authority comparison, not an independent agent benchmark or speed
measurement. Model turns/time are unknown; script wall time is only fixture execution.
Four corrupted transcripts fail: fabricated fact, stranger recipient, resend after
unknown, send after cancellation. The runner validates actual gateway input schemas;
it makes no clinical, correlation or conversation decisions.

Native fake-dependency tests passed: TestNativeTextPlainLinkAndQuoteAreExact,
TestNativeTextValidatesBeforeSend, TestNativeTextChecksRevocationAfterResolvingReply,
TestNativeSendNeverRetriesAmbiguousOutcome, TestNativeSendRejectsUnprovenReceiptIdentitiesOnce.
No authenticated client is constructed. These prove existing native behavior only.

An independent Codex surrogate exercise uses `halodoc_caller_fixture.py` in an isolated
fixture directory, with the skill/context and no prewritten choices. Its actual call
log and measured result are recorded separately under docs/experiments/halodoc-order/.
It is not Grok, a Terra phone run, a live doctor, real prescription or WhatsApp delivery.

The [actual caller trace](../experiments/halodoc-order/caller-actual-calls.json) has
27 fixture calls (17 gateway-schema calls), two correctly bound quoted replies and
three owner questions. It preserved one patient/order context, answered known duration,
returned unknown allergy history, retained the physician artifact, excluded stranger/
other-order messages, reconciled the lost send by original request_id, and sent nothing
after cancellation despite a later inbound instruction. [Validation](../experiments/halodoc-order/caller-validation.json)
passed these outcomes; [report](../experiments/halodoc-order/caller-report.json) records
unresolved delivery/entrance and uncertainty. Model identity, elapsed time and turns are
unknown. No 3→1 improvement is inferred from this independent run.

Observed friction: two owner questions concerned the entrance; the fixture owner
returned an irrelevant allergy answer. The skill now suggests asking the bound delivery
participant to clarify which entrance serves the confirmed reception. This narrow
refinement is not independently rerun; [the exact tested skill](../experiments/halodoc-order/caller-skill-used.md)
is retained. The fixture tests caller decisions, not Android UI/doctor behavior or wakes.

Live acceptance remains ANYM-994 criterion 7: an actual owner-scoped order, physician
consultation/prescription evidence, pharmacy/delivery and one correctly bound WhatsApp
reply returned to Grok's same context, with honest receipt evidence and unknown-question
handoff. No whole-journey speed or deep-link benefit is claimed before that test.
Undo: revert the source commit and stop loading the staged caller skill. No production
or phone state was changed.
