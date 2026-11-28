# Existing BB conversation through DeedKit MCP

The owner uses Grok in mobile Devkit. The existing GoSync MCP endpoint forwards
`android_task` to the existing Android service, which sends the owner's original
words to one configured BB Codex conversation on the USB host. Its thread ID is
the returned `task_id` for goals, clarifications and results. BB's existing atomic
queue remains the only conversation manager. No new agent, queue, public listener,
native session, model selection or authentication is created.

## Caller contract

Use `action=message` with `device_id`, one original `request_id` UUID and `text`.
The text goes unchanged on stdin to BB's `thread tell --mode auto`: BB steers the
active conversation or queues/starts its next turn in that same conversation.
An ordinary clarification is another message to that conversation. The agent
interprets its meaning using the existing history.

When delegating a goal, give the desired outcome, facts the phone agent needs and
the owner's actual constraints. The phone agent chooses the app path, tools and
whether it needs a screenshot. Include an exact payment method, a screenshot
deliverable or specific UI steps when the owner asks for them. Keep checkout or
payment limits as given. For example, “Prepare the Grab cart for the listed
items within Rp150,000. Stop before placing the order.”

`action=status` reads BB metadata; `action=result` reads the latest conversation
output only when idle with no queued messages. The latter is explicitly scoped
`latest_conversation_result`, not an independent order-completion receipt.
Neither operation sends a message or starts a model. MCP discovery and the task
capability check also read configuration only, with no ADB or engine probe.
Unobserved phone state is omitted rather than reported as free or disconnected.

The private local send receipt stores the original UUID and payload digest, not
the text. A repeated identical delivery returns that receipt; conflicting text is
refused. `delivery=unknown` means the CLI reply could not prove acceptance. Read
`action=receipt` with the original UUID; never resend unknown input, invent a new
UUID or switch to another client. This is transport bookkeeping, not a second
task scheduler or semantic deduplication system.

The configured current principal must still have control of the configured device.
Project, thread, environment, machine, workspace, provider and CLI hash are checked
before sending. Revoked rights, a changed binding or an unavailable target refuse
without creating a replacement. Existing BB/phone exclusive ownership and queue
rules continue to apply. No MCP task call itself acquires a phone lease.

## Configuration and switch

The Android config accepts an optional `bb_conversation` object, absent by default:

```json
{
  "principal_id": "existing-backend-principal",
  "device_id": "existing-device",
  "node": "/absolute/path/to/node",
  "cli": "/absolute/path/to/bb",
  "cli_sha256": "64-lowercase-hex-from-the-installed-cli",
  "server_url": "http://127.0.0.1:38890",
  "thread_id": "existing-thread",
  "project_id": "existing-project",
  "environment_id": "existing-environment",
  "host_id": "existing-machine",
  "workspace": "/absolute/existing/workspace"
}
```

Use the already authorized principal/device and the verified existing BB target;
do not copy credentials into this object. Preserve existing native/model pins,
history, namespaces, receipts and grants. Install the accepted green-main Android
wheel and gateway through the normal release paths after fresh work/call checks.
Retain exact prior package/config/policy and the normal executable undo.

The live switch is adding that object to the private Android configuration and
adding `android_task` to the intended existing gateway profile in a new policy
revision. Keep the same backend URL, credentials and public MCP registration.
Retain the old tool path while verifying tools/list, phone-free status and original
receipt handling through the real admitted caller. Configuration success alone
does not prove that mobile Grok selected or used the tool.

Then, with fresh phone ownership and one concrete owner goal, verify Grok → same
Codex → basket before checkout → clarification/status/result → Grok. Do not invent
products, address or budget. Basket preparation does not authorize payment or
placing the order. Verify busy/unknown/repeat behavior on synthetic tests, not a
real purchase. Remove the old caller tool path only as a separate final step after
replacement acceptance; do not stop or delete the original conversation.

Undo: restore the prior tool membership in a new, higher gateway policy revision,
preserving concurrent fields with the normal comparison checks; never lower an
accepted revision. Remove the optional `bb_conversation` config object. If reverting binaries, first remove the tool from
the profile, then restore the accepted prior package through normal lifecycle
checks. Preserve the state database, BB history and send receipts; never restore
an old database to undo a code change. No live patch of immutable releases.
