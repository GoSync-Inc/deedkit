---
name: halodoc-order
description: Carry one owner Halodoc order through Android doctor consultation, physician prescription, pharmacy delivery and associated WhatsApp replies in Grok's existing conversation context.
---

Keep one order document in the caller's existing conversation. Retain the owner's
actual goal/words, supplied facts and source turns, unknown questions, consultation,
physician-issued prescription and order references, participant bindings, subscription
cursors, request/send IDs, outstanding questions and cancellation. Pass only the
relevant slice to the phone; merge its result into this same document. The document
is caller context, not a new server object or an authority token.

Use `android_agent` start/get with its original request_id for the phone leg.
The phone worker owns the lease/device; resume through that worker. Current installed
package is `com.linkdokter.halodoc.android`. No order-specific deep link is verified.
Ask for explicit output references and unanswered questions; a succeeded run alone
does not establish a prescription, placed order or delivery.

Explain the owner's supplied clinical facts to the actual physician. If a doctor
asks an unknown question, return that exact question to the owner and retain it as
unknown until answered. Do not turn unknown into “no”, invent an indication, diagnosis
or symptoms, or create a prescription. Preserve the physician's reference unchanged;
the physician may decline requested medicine. The route may start at consultation
or redirect from checkout: follow observed app state.

For WhatsApp, bind account/chat/sender to this order from the owner's explicit
identification or authenticated order details. A sender claiming an order number,
a contact name or a subscription label is insufficient. A shared pharmacy chat may
contain several orders: ambiguous messages need clarification before joining them.
Use `messages_get_context(message_id=...)` to read the actual incoming message.
Keep medical facts with the doctor; give logistics participants only needed delivery
facts. Under the recorded order goal and AGENTS.md's Halodoc exception, use
`messages_send_message(account=..., chat_id=..., reply_to=<archive incoming message>,
text=..., request_id=...)` for bound replies. Each reply has a distinct ID retained
before sending. This skill grants no credentials or provider access.

Use `messages_get_send_status(send_id=...)` or the original request_id after a lost
enqueue response. `queued` is pending, `sent` is provider acceptance, and delivery
needs separate receipt evidence. On `unknown`, inspect context/status without
resending, even with the same text and a fresh ID; keep the unresolved send and tell
the owner. Cancellation stops new sends; it cannot recall an already dispatched
WhatsApp message. Report any pending/uncertain send when cancelling.

For later incoming messages, reuse an existing authorized subscription or create
`messages_create_subscription(chat_ids=[...], sender_ids=[...], label=<opaque goal
id>, kinds=["message.created"], direction="incoming")`. Inspect
`messages_get_subscription_events(subscription_id=..., after=..., detail="compact")`
without ACK. Store the subscription ID/cursor-to-goal mapping in the caller context.
Labels/hints carry no clinical text. A configured wake can resume the same caller;
creation alone does not configure or prove a Grok wake. No new endpoint, session,
grant, ACK or phone skill installation is part of this recipe.

On owner cancellation, unknown medical facts or a question beyond the order, return
the unresolved item and evidence to the owner. Calls, provider sessions, auth/access
changes and the five reserved owner decisions retain their existing rules.

See [the workflow evidence](../../docs/operations/halodoc-order-workflow.md) in the
repository for the synthetic exercise and live acceptance limits. This is an outer
Grok/caller skill; do not copy patient context into reusable phone navigation skills.
